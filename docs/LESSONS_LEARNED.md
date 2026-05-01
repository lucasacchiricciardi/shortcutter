# Lessons learned

Five hard-won lessons from one intense day of building a multi-script LLM pipeline. Documented while still fresh.

## 1. Small LLMs hallucinate predictably

**Problem.** When a 4B-parameter model can't find a GitHub repo URL in the description, it doesn't say "I don't know" — it invents one. Always with `anthropics/<tool-name>` as the prefix, regardless of who actually maintains the repo. We saw this multiple times: `anthropics/ccusage`, `anthropics/llmsizer`, etc. None of those exist.

**Anti-pattern.** Trying to fix this with a stricter system prompt ("don't invent URLs!"). The model will agree, swear by it, and then invent the URL anyway in the next call.

**Solution.** Don't trust, validate. After the LLM returns a URL, do a `HEAD` request:

- If 200 → URL exists, keep it.
- If 404 → URL is hallucinated, delete it and penalize the confidence score.
- If timeout/other → tolerant, keep the URL but flag it.

A 30-line function eliminated a class of hallucinations completely.

## 2. Short transcripts break the fixer

**Context.** I built an LLM-based "transcript fixer" that takes Whisper's raw output and corrects acoustic errors using the video description as ground truth (e.g. "Cloud Code" → "Claude Code", "C-usage" → "ccusage"). On a 3,400-character news digest transcript, it produced 19 perfect corrections.

**Problem.** On a 1,146-character short video transcript, the same fixer with the same prompt produced 0 useful fixes — and on multiple attempts, it tried to *replace* the transcript with the description, generating completely fabricated content.

**Why.** The system prompt is large (~110 lines with examples). When the actual user content is short, the model's "attention budget" is dominated by prompt engineering rules, not by the small text it's supposed to fix. It pattern-matches the description (also in the prompt) as the "expected output" and regurgitates it.

**Solution.** Threshold: skip the fixer entirely for transcripts under 1,500 characters. Use Whisper raw and let the downstream cross-check LLM (which has different framing) do the heavy lifting.

## 3. A 5-line sanity check is gold

**The disaster avoided.** As mentioned above, the fixer once tried to replace the audio transcript (1,146 chars) with the description (an entirely different text, but similar length). Without a guard, this corruption would have propagated downstream into news extraction, summary generation, and the user-facing email digest.

**The guard.**

```python
ratio_change = abs(len(corrected) - len(transcript_raw)) / max(len(transcript_raw), 1)
if ratio_change > 0.30:
    print(f"⚠️  Transcript differs {ratio_change*100:.0f}%. Suspect, using raw.")
    return transcript_raw, [], elapsed
```

Five lines. Saved the project.

**Generalization.** Sanity checks aren't paranoia — they're a contract. They say: *"Even if the LLM behaves outside expected parameters, my pipeline degrades gracefully instead of producing corrupt output."* In an LLM-based system, this is the difference between robust and brittle.

## 4. Sometimes the bug is upstream

**The case.** During testing, one video produced an output that looked completely wrong — title said "Open Generative AI" but the transcript talked about labor market AI displacement and the visual frames showed a research paper. My first instinct: bug in my pipeline.

**Three hours of debugging later.** Confirmed: yt-dlp downloads the correct video (verified by `ffprobe`, file size, duration, title metadata, manual file extraction). Whisper transcribes the actual audio it was given. Vision analyzes the actual frames. **Everything works correctly. The video itself was published with mismatched audio.** The creator uploaded the wrong audio track.

**Lesson.** When your output is incoherent and the upstream data is corrupt, the most honest behavior is to *report the incoherence*, not to invent a summary that "makes sense". My pipeline noted this honestly: it identified the resource from the description ("Open Generative AI") but the cross-check LLM correctly didn't pretend the audio matched.

A pipeline that says "I don't know" when it shouldn't know is worth more than one that always sounds confident.

## 5. The 3 rules of Sacchi

Three rules I've been teaching to my CCNA students for years. They saved this project today more than any prompt or model:

### 🛡️ Safety first

Always design for the failure case. Sanity checks. Validations. Fallbacks. Default-deny semantics. *"What's the worst output the LLM could produce, and what stops it from reaching the user?"*

### 🔁 Little often

This pipeline went through **9 iterative versions in a single day**. Each version had one clear goal, was tested in isolation, and either committed or rolled back. No big-bang refactors. No "I'll fix all five things at once". The 9th version is robust because it's the result of 9 directed improvements, not 1 ambitious leap.

### 👁️ Double check

The most embarrassing bug of the day: a Python variable shadowing issue inside the fixer function. The outer scope had `corrected = transcript_corrected` (the full 3,400-char transcript). The inner loop did `corrected = fix.get("corrected", "")` (a single word). Loop runs, outer variable is overwritten with the *last* fix's word.

Final output: `transcript_corrected = "ola"` — three characters, the literal last word.

The pipeline ran end-to-end. No exceptions. JSON valid. Markdown report generated.

It would have shipped silently corrupted, except a careful look at the report flagged "wait, the transcript is suspiciously short". Investigation found the bug in 5 minutes. Without that double check, this would've gone to production.

```python
# BEFORE (bug):
corrected = parsed.get("transcript_corrected")  # outer scope
for fix in fixes_raw:
    corrected = fix.get("corrected", "")  # ← shadows outer!
return corrected, fixes  # ← returns last fix's word, not transcript

# AFTER (fixed):
transcript_corrected = parsed.get("transcript_corrected")  # rename outer
for fix in fixes_raw:
    fix_corrected = fix.get("corrected", "")  # ← distinct
return transcript_corrected, fixes  # ← correct
```

**Moral.** LLMs hallucinate. So do programming languages, in their own way. Trust nothing. Verify everything. Especially yourself.

---

## Closing thought

When you build a system on top of LLMs, you can't hope the model "behaves well." You assume it will misbehave and design resilience around it.

The robust pipeline is the one that intercepts its own errors before your users do.
