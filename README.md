# Provenance Guard

Provenance Guard is a Flask API that estimates whether writing submitted to a forum looks AI-generated or human-written. It shows the evidence behind the estimate and lets a creator ask for a review.

This is an estimate, not proof of who wrote something. I built the project to
make that uncertainty visible rather than present an automated score as fact.


## Architecture overview

A creator sends text and a creator ID to `POST /submit`. The API validates the
request and applies the per-IP rate limit, then sends the text to both the
Groq classifier and the Python stylometric heuristic. It combines their
AI-likelihood scores using the documented 78/22 weighting, maps the combined
score to a result band, generates the matching transparency label, and records
the decision and evidence in SQLite.

If the creator appeals with `POST /appeal`, the API links the appeal to the
original decision, records the creator's reasoning, and changes the submission
status to `under_review`. The API returns the appeal receipt; it does not
perform a human review itself.

## How the detection works

I use two signals that use different properties of a passage:

1. **Groq classification** looks at the text as a whole and returns an
   `ai_likelihood` from 0 to 1 with a short reasoning. This gives the project
   a semantic and stylistic assessment, but the model cannot know who actually
   wrote the text or what editing it went through.
2. **Stylometric heuristics** measure sentence-length variability, type-token
   ratio, and punctuation density. A small Python function combines these
   features into its own `ai_likelihood` from 0 to 1. It measures patterns in
   the writing's structure, not meaning. For text shorter than 10 words, it
   returns a neutral score of `0.5` because there is too little text to measure
   patterns effectively.

### Combined score and confidence

The combined AI-likelihood weights the Groq estimate more heavily:

```text
combined_ai_likelihood = 0.78 * llm_ai_likelihood
                       + 0.22 * stylometric_ai_likelihood
```

Confidence is a separate evidence-strength heuristic:

```text
disagreement = abs(llm_ai_likelihood - stylometric_ai_likelihood)
confidence = 0.5 + abs(combined_ai_likelihood - 0.5) * (1 - disagreement)
```

Confidence rises when the combined estimate moves away from an even split and
falls when the two signals disagree. A confidence of `0.6` does not mean the
system is right 60% of the time. It is an evidence-strength heuristic, not a
calibrated probability.

The combined score determines the result:

- **`likely_human`:** below `0.35`.
- **`likely_ai`:** above `0.75`.
- **`uncertain`:** from `0.35` through `0.75`, including both boundaries.

The Groq score contributes 78% to the combined result and stylometry contributes
22%. Confidence and signal disagreement are still returned to help explain a
result, but they do not override these score bands. I tested that the scores
vary across fixed examples and that each score band behaves as expected. These
tests check the implementation; they do not establish real-world accuracy or
calibration.

### Example scores from tests

I ran the detector on two test passages using a fixed classifier, so these
examples are repeatable. The classifier values are test inputs, not live Groq
predictions.

| Test case | LLM score | Stylometric score | Combined AI-likelihood | Confidence | Result |
|---|---:|---:|---:|---:|---|
| Irregular first-person story | 0.00 | 0.0039 | 0.0009 | 0.9972 | `likely_human` |
| Formal passage about AI's role in society | 0.99 | 0.3548 | 0.8503 | 0.6278 | `likely_ai` |

The high-confidence test submission was:

> I left at dawn. Why? The station was empty, except for one dog, who seemed
> to recognize me; or maybe he just wanted breakfast. Anyway, I missed the
> bus. Twice, actually, because I forgot the date, lost my keys, and circled
> back through the rain before the bakery opened. By then, the whole plan had
> changed.

The lower-confidence test submission was:

> Artificial intelligence represents a transformative paradigm shift in
> modern society. It is important to note that while the benefits of AI are
> numerous, it is equally essential to consider the ethical implications.
> Furthermore, stakeholders across various sectors must collaborate to ensure
> responsible deployment.

The first example has a high confidence heuristic and strong agreement near the
human end of the range. The second has a lower confidence heuristic: the LLM
score is high, but the stylometric score is much lower. Its combined score
still clears the `0.75` AI cutoff. The difference between `0.9972` and `0.6278`
shows the confidence heuristic varies, but it does not prove that either
estimate is calibrated or correct.

## Transparency labels

Each submission returns a typed label and the exact display text. `{N}` below
is the confidence score rounded to the nearest whole percent.

- **Likely AI (`likely_ai`):** `Likely AI-generated ({N}% confidence). Automated detection can be wrong; the creator may appeal.`
- **Likely human (`likely_human`):** `Likely human-written ({N}% confidence). Automated detection can be wrong.`
- **Uncertain (`uncertain`):** `Uncertain ({N}% confidence). We couldn't confidently determine whether this text is AI-generated or human-written.`

The implementation's typed variants are `likely_ai`, `likely_human`, and
`uncertain`. The assignment describes a high-confidence AI variant; this
project currently calls it `likely_ai` because its AI score band does not
require a high confidence value. I am keeping that wording accurate rather
than claiming high confidence when the rules do not require it. The three
variants are covered by tests, and the label follows the result category.

## Try the API

### Submit text

`POST /submit` requires a non-empty `text` string (up to 50,000 characters)
and a non-empty `creator_id` string (up to 200 characters). In PowerShell:

```powershell
$body = @{
  text = "The sun dipped below the horizon. I watched from the porch as the neighborhood grew quiet."
  creator_id = "test-user-1"
} | ConvertTo-Json

$submission = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:5000/submit `
  -ContentType "application/json" `
  -Body $body

$submission | ConvertTo-Json -Depth 10
$contentId = $submission.content_id
```

A successful request returns HTTP `201` and includes the `content_id`,
`attribution`, `confidence`, `combined_ai_likelihood`, label, and both signal
outputs. `result` is also returned as an alias for `attribution`. The API
stores the decision and a SHA-256 hash of the text, not the submitted text
itself.

### Appeal a decision

Use the `content_id` from the submission response:

```powershell
$appealBody = @{
  content_id = $contentId
  creator_reasoning = "I wrote this myself and can provide drafts."
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:5000/appeal `
  -ContentType "application/json" `
  -Body $appealBody
```

A successful appeal returns HTTP `201`, confirms receipt, and changes the
submission status to `under_review`. The API records the appeal reasoning and
links the event to the original decision. It does not automatically
reclassify the text. `/appeals` is also supported for compatibility, as is
`reasoning` as an alternate request field. An unknown content ID returns
`404`; a duplicate appeal while it is under review returns `409`.

### Inspect the audit log

`GET /log` returns structured SQLite audit events, newest first:

```powershell
$log = Invoke-RestMethod -Method Get -Uri http://127.0.0.1:5000/log
$log | ConvertTo-Json -Depth 10
```

Each decision event includes its timestamp, content and decision IDs, creator
ID, attribution, combined score, confidence, both signal outputs, label, and
status. An appeal event includes the same decision evidence, the
`appeal_reasoning`, and `under_review` status.

Ex:

```json
{
  "count": 3,
  "entries": [
    {
      "event_type": "appeal",
      "content_id": "id-1",
      "decision_id": "id-1",
      "creator_id": "test-user-1",
      "timestamp": "2026-10-24T13:20:00Z",
      "attribution": "uncertain",
      "combined_ai_likelihood": 0.5078,
      "confidence": 0.5077,
      "llm_score": 0.51,
      "status": "under_review",
      "appeal_reasoning": "I wrote this piece and can provide my drafts.",
      "signals": {
        "llm": {"ai_likelihood": 0.51, "rationale": "Evidence is mixed."},
        "stylometric": {
          "ai_likelihood": 0.5,
          "evidence": "Insufficient text for a useful stylometric estimate."
        }
      }
    },
    {
      "event_type": "decision",
      "content_id": "id-2",
      "decision_id": "id-2",
      "creator_id": "test-user-2",
      "timestamp": "2026-10-24T13:10:00Z",
      "attribution": "likely_human",
      "combined_ai_likelihood": 0.1724,
      "confidence": 0.69,
      "llm_score": 0.08,
      "status": "classified"
    },
    {
      "event_type": "decision",
      "content_id": "id-1",
      "decision_id": "id-1",
      "creator_id": "test-user-1",
      "timestamp": "2026-10-24T13:00:00Z",
      "attribution": "uncertain",
      "combined_ai_likelihood": 0.5078,
      "confidence": 0.5077,
      "llm_score": 0.51,
      "status": "classified"
    }
  ]
}
```

These IDs, timestamps, and scores are illustrative data to show the response
shape. A real `/log` response includes the timestamp, label fields, and
complete signal evidence. The local audit endpoint is not access-controlled.

## Rate limits

I set both `POST /submit` and `POST /appeal` (also `/appeals`) to **5 requests
per IP per hour**. This allows a person to submit and appeal occasional work
while limiting bursts of expensive Groq requests. Flask-Limiter uses
`memory://` storage here, so counters reset when the app restarts.

## Known limitations and what I would change

- **Genre and writing style can fool the heuristics.** A poem or song lyric
  with a repeated refrain and simple vocabulary may get a high stylometric
  AI-likelihood even when a person wrote it. The heuristic rewards regular
  sentence lengths and repeated vocabulary without understanding why they
  occur.
- **Short passages do not give the heuristic much to work with.** Under 10
  words, it returns `0.5`; the combined result then depends more on the LLM
  score.
- **The numbers are not calibrated.** I have not evaluated the system on a
  representative set of labeled human- and AI-written text. For a real
  deployment, I would build that evaluation set, measure false-positive and
  false-negative rates across genres and language backgrounds, calibrate the
  scores, and review thresholds before showing verdicts to users.

## Spec reflection

The specs helped me turn a broad idea into specific API model:
they directed me to define score ranges, thresholds, label text, appeal
statuses, and audit fields before building the endpoints. That made it easier
to test the workflow end to end instead of only checking whether Flask ran.

I kept SQLite rather than switching to the tutorial's simpler JSON-lines log
because the project already had linked decision and appeal events, and SQLite
makes that relationship easier to query. I also kept the API's `201 Created`
responses and the `/appeals` compatibility route rather than copying every
example literally.

## How I used AI tools

1. **Planning the design:** I asked an AI assistant to improve the planning
   document and make its detection and uncertainty choices implementation
   ready. It helped organize the signal descriptions, score thresholds, label
   variants, appeal flow, and architecture. I kept the language tied to the
   actual project and revised the confidence formula so the documented range
   matched the implementation.
2. **Reducing the uncertain range:** I asked the assistant to address how often
   results were landing in the uncertain category. I wanted clearer human and
   AI score bands, but also wanted to stay cautious rather than lower the AI
   cutoff too far. We kept the 78/22 signal weighting and set the combined
   score bands to below `0.35` for likely human, above `0.75` for likely AI,
   and the range between them for uncertain.

## Youtube Link
https://youtu.be/8EYWy8xCP9o
