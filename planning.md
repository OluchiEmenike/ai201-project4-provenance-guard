# Project Planning

## Goal

This project estimates whether submitted text in a platform such as twitter, reddit or good reads looks AI-generated or human-written, shows the evidence and uncertainty behind that estimate, and gives creators an auditable way to appeal. The result is an estimate not actually proof of authorship.

I want the tool to be clear about what it measures and where it can get things wrong. A high score doesn't alwaus show proof that someone used AI.

## Detection signals

Every successful submission must use both signals. If either signal is unavailable, the app should return an error rather than just making a decision from only one signal.

1. **LLM classification (Groq):**
 estimates AI likelihood from semantic and stylistic patterns in the text and returns a short rationale. Its output is an `ai_likelihood` number from 0 to 1 plus the reasoning. One limitation is that it cannot know the writer's context and may misread genre, editing, or a writer's actual style.

2. **Stylometric heuristics (Python):** estimates AI-likelihood from sentence-length variability, type-token ratio, and punctuation density. Its output is an `ai_likelihood` number from 0 to 1, the measured features, and an explanation. It measures structural regularity, not meaning, and is sensitive to genre and length. Text shorter than 10 words receives a neutral score of 0.5 because there is not enough evidence.

The signals are combined into one AI-likelihood:

The LLM contributes 78% and the stylometric signal contributes 22%. An overall score of 0.8 is the system's cutoff for a `likely_ai` result.

## Uncertainty 

The API returns both the combined AI-likelihood and a separate `confidence` score from 0 to 1. Confidence is an evidence strength measurement: 0.6 means the current signals provide limited support for the system's direction, not that the system is correct 60 percent of the time. Confidence increases as the combined score moves away from 0.5 and falls when the signals disagree.

Use this bounded formula for the confidence estimate:

`disagreement = abs(llm_ai_likelihood - stylometric_ai_likelihood)`

`confidence = 0.5 + (abs(combined - 0.5) * (1 - disagreement))`

This keeps confidence between 0.5 and 1.0 for valid signal scores. For example, a confidence of 0.6 means the combined estimate is only slightly deviating from an even split after accounting for agreement. It does not mean the system is correct 60% of the time.  

The score bands can be summarized as:

- **`likely_human`:** combined AI-likelihood is below `0.35`.
- **`likely_ai`:** combined AI-likelihood is above `0.75`.
- **`uncertain`:** scores from `0.35` through `0.75` 

The confidence heuristic and signal disagreement remain in the response as context, but they do not override these score bands.

## Transparency labels

The API returns a typed label (`likely_ai`, `likely_human`, or `uncertain`) and the matching display text.  It is important that cautious language is used, and that an estimate isnt taken as proof, and avoid presenting an uncertain result as a final verdict.
 
## Appeals workflow

- **Who can appeal:** the creator of a submitted text. The local API does not authenticate identity, so it accepts an appeal from anyone who knows the submission's `content_id`. This is a limitation of this project.
- **What they submit:** the `content_id` and a `creator_reasoning` string explaining why they are requesting review. The API also accepts the existing `reasoning` field to check for compatibility. A submission requires a `creator_id`, but the local API does not verify it. The API does not accept attachments.inks
- **What the system does:** `POST /appeal` validates the request, stores the creator's reasoning, and changes the content status to `under_review`. It does not automatically rerun detection or claim that a human review has happened. Unknown IDs return 404; a duplicate appeal while under review returns 409.

- **What gets logged:** the original decision event retains the result, combined AI-likelihood, confidence, label, both signal outputs, creator ID, status, and timestamp. The appeal event records the content and decision IDs, creator ID, creator reasoning, status, and timestamp. The submitted text itself is not stored so the audit log stores a hash instead.

- **What a reviewer should see:** an appeal queue should show the pending status, appeal time, creator's reasoning, linked result and confidence, exact label, both signal scores and their rationales/evidence, measured stylometric features, and the text hash. The text is not available in this local system, so the reviewer cannot reread it unless the creator adds is in the reasoniing section.
## Anticipated edge cases

- **A repetitive poem or refrain:** repeated lines and simple vocabulary can lower sentence-length variability and type-token ratio, making a human poem look more AI-like to the stylometric signal.
- **A short submission:** a one-sentence answer or short poem may have too little evidence for meaningful stylometry; the heuristic returns a neutral 0.5, which can make the combined result depend heavily on Groq.
- **Edited or formulaic human writing:** a carefully edited report, application essay, or non-native English writing may have regular phrasing that the LLM or heuristics interpret as AI-like.
- **AI text that has been heavily rewritten:** personal details and editing can make AI-generated text look less regular, lowering the stylometric estimate and causing disagreement.
 

## Architecture

```text
Creator
   |
   | POST /submit: text
   v
Flask API -- validate text + apply per-IP rate limit
   |
   v
Detector
   |------------------------------|
   v                              v
Groq LLM signal             Python stylometric signal
score + rationale           score + features/evidence
   |------------------------------|
                  |
                  v
     Weighted score + confidence
        + result + label
                  |
        +---------+---------+
        |                   |
        v                   v
 SQLite audit log      Response to creator
 (decision + hash)      (scores + label)
        ^
        |
Creator -- POST /appeal: content_id + creator_reasoning
        |
        v
 Record appeal event; set status to under_review
        |
        +---- GET /log: ordered decision and appeal events
```



If Groq is missing or unavailable, submission fails explicitly instead of using only the stylometric signal.

## AI Tool Plan

### M3 — Submission endpoint and first signal

- **Spec to provide:** the detection signals section and the Architecture diagram, including the requirement to return an explicit error if a required signal fails.
- **Ask the AI tool to generate:** a Flask app skeleton with `POST /submit` and the first signal function, the Groq LLM classifier returning a 0–1 AI-likelihood and short rationale.
- **Verify before wiring it into the endpoint:** call the classifier directly with a clearly AI-like passage, a clearly human-like passage, and a short passage. Check that each response has the expected fields and a finite score between 0 and 1.

### Second signal and confidence scoring

- **Spec to provide:** the detection signals and uncertainty representation sections plus the Architecture diagram.
- **Ask the AI tool to generate:** the Python stylometric function and the scoring logic that combines both signals and assigns one of the three results.
- **Verify:** run both signals on clearly AI-like and clearly human-like samples. Confirm scores vary in the expected direction, short text receives the documented neutral stylometric score, confidence stays between 0 and 1, disagreement reduces the confidence heuristic, the audit log records each signal and the combined score, and scores below 0.35 or above 0.75 reach the matching result even when the signals disagree. 

### Production layer

- **Spec to provide:** the transparency label variants and Appeals workflow section plus the Architecture diagram.
- **Ask the AI tool to generate:** label-generation logic and the appeal endpoint, including validation, status changes, and audit-log events. Support the milestone's `POST /appeal` shape while keeping `/appeals` as a compatibility alias for existing clients.
- **Verify:** test that all three typed label variants and exact display strings are reachable at their thresholds. Submit an appeal for a known decision and confirm the status becomes `under_review`, the appeal is linked and logged with the reasoning, and unknown or duplicate appeals return the documented errors.

## Work plan

1. **Submission and detection:** validate text, collect both independent signals, combine them , and return the result, confidence, and label.
2. **Appeals and auditability:** record each decision and creator appeal and move appealed content to `under_review`.
3. **Abuse controls:** apply rate limits to submissions and appeals.
4. **Verification and documentation:** test signal behavior, confidence, labels, API errors, appeals, and audit entries; document limitations.

## Completion criteria

- Every successful decision uses both Groq classification and stylometric features.
- The response includes the combined AI-likelihood, confidence, typed transparency label, and both signal results.
- A combined score below 0.35 produces `likely_human`, above 0.75 produces `likely_ai`, and the inclusive middle band produces `uncertain`.
- Appeals capture creator reasoning, reference the original decision, and set status to `under_review`.
- SQLite audit events cover decisions, scores, signals, labels, and appeals without storing submitted text.
- Submission and appeal endpoints enforce documented rate limits.
 
