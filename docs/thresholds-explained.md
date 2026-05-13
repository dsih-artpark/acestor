# Understanding Thresholds — From First Principles

> This document builds intuition from scratch.
> No formulas until we need them. No code at all.
> Each section only adds one new idea.

---

## Part 1 — The core problem

### Imagine you are a district health officer

It is Monday morning. Your surveillance system reports:

> **Kurnool district: 50 dengue cases this week.**

Is that bad? Should you act?

You cannot answer that question with just the number 50. You need context.

- Is 50 cases normal for Kurnool?
- Is this the rainy season, when dengue always peaks?
- Was it 10 last week and 50 this week — a sudden jump?
- Or has it been 40–60 every week for months?

**The number alone tells you nothing. You need a reference point.**

That reference point is called a **threshold**.

---

## Part 2 — What a threshold actually is

A threshold is the answer to: **"what is normal here, at this time of year?"**

Once you know what is normal, you can say:
- Below the threshold → normal, keep monitoring
- Above the threshold → unusual, investigate or act

A good threshold has two properties:

1. **It is specific to the place.** What is normal in Kurnool is not normal in Srikakulam.
2. **It is specific to the time of year.** What is normal in July (monsoon peak) is not normal in January.

---

## Part 3 — The simplest approach: historical average

The most natural reference point is the past.

> "In the last 4 years, Kurnool in week 28 had an average of 35 cases."

So 50 cases this week is higher than the historical average of 35. That is a signal.

But averages alone are not enough. Consider two districts:

| District | Historical average, week 28 | Typical range |
|----------|----------------------------|---------------|
| Kurnool | 35 | 30 – 40 |
| Visakhapatnam | 35 | 10 – 60 |

Both have the same average. But 50 cases means something very different in each:
- In Kurnool, the range is tight. 50 is way above the usual band. Alarming.
- In Visakhapatnam, the range is wide. 50 is well within normal variation. Not alarming.

**So we also need to measure how much the counts typically vary.** That is the standard deviation (SD). You do not need to know the formula — just think of it as: *how wide is the normal band?*

---

## Part 4 — Turning average + variation into a threshold

Combine the average (mean) and the variation (SD) like this:

```
Threshold = Mean + (some multiplier × SD)
```

The multiplier controls how sensitive you want to be:

- **× 1** → catches more cases, but also more false alarms
- **× 2** → a good balance. Only about 5% of weeks would exceed this by chance.
- **× 3** → very conservative. Only ~0.3% of weeks would exceed this by chance.

This gives you a **risk classification**:

| Predicted cases | Zone | Label |
|----------------|------|-------|
| Below Mean | 1 | Low |
| Mean to Mean + 1×SD | 2 | Moderate |
| Mean + 1×SD to Mean + 2×SD | 3 | High |
| Above Mean + 2×SD | 4 | Very High |

This is the **WHO method**. It answers: *"is this district unusual compared to its own past?"*

---

## Part 5 — Where does the historical data come from?

To compute Mean and SD for "Kurnool in week 28," you look back at previous years:

- Week 28 of 2024: 32 cases
- Week 28 of 2023: 38 cases
- Week 28 of 2022: 34 cases
- Week 28 of 2021: skip (COVID disrupted surveillance)

Mean ≈ 35. SD ≈ 3. Threshold = 35 + 2×3 = 41.

If this week has 50 cases → above 41 → High risk.

This is what the pipeline calls **`historical`** — the same-week, same-district historical baseline.

---

## Part 6 — The cold start problem

The historical method needs at least one prior year of data for the same week.

What if a district is newly added to the system? Or data collection only started 3 months ago?

In that case, there is no historical baseline to compare against. The historical method cannot run.

The fallback is to use **recent data instead of same-season data**:

> "I don't have last year's week 28. But I have the last 4 weeks."
> Mean = average of the last 4 weeks. SD = variation in those 4 weeks.

This is less ideal — it compares this week to recent weeks, not to the same season last year. But it is far better than having no threshold at all.

This is what the pipeline calls **`prev_nweeks`** — the previous-N-weeks rolling baseline. It is a fallback for cold-start districts.

---

## Part 7 — Pause: what we have so far

Both methods above answer the **same question** in slightly different ways:

> "Is this district unusual compared to its own history?"

They are **absolute and time-aware**:
- Absolute: they compare case counts to a number (Mean + n×SD)
- Time-aware: what is "normal" changes by week of year

They produce the same 4-tier output: Low / Moderate / High / Very High.

The pipeline computes **both** every run and uses `assess_thresholds` to pick whichever method is performing better for that week.

---

## Part 8 — A completely different question

Imagine you are the state health secretary for Andhra Pradesh.

You have 26 districts. You have a budget for emergency response teams. You can deploy to 6–7 districts this week.

The WHO method tells you which districts are **unusual vs their own past**. But it does not directly answer: **"which districts need my attention most, right now, compared to each other?"**

For resource allocation, you do not care whether Kurnool is 1.5 standard deviations above its historical mean. You care that **Kurnool has more cases than 80% of other districts this week.**

This is the **ICMR method**. It answers a different question:

> "Where does this district rank among its peers right now?"

---

## Part 9 — How ICMR works

Take all 26 AP districts. Look at their predicted case counts for this week.

Sort them from highest to lowest. Divide them into 4 equal groups:

| Group | Name | Meaning |
|-------|------|---------|
| Top ¼ | A1 Critical | Highest burden districts — act now |
| Next ¼ | A2 High | High burden — prioritise |
| Next ¼ | A3 Caution | Elevated — monitor closely |
| Bottom ¼ | A4 Low | Lowest burden this week |

**Worked example:** 26 districts → 6–7 per group.

Kurnool has 50 cases. If 6 other districts have more than 50, Kurnool is A2. If only 2 districts have more than 50, Kurnool is A1.

The 50 cases hasn't changed. But the interpretation changes entirely depending on what the other districts look like.

---

## Part 10 — The key difference between WHO and ICMR

This is the most important thing to understand:

| Property | WHO (historical) | ICMR (quartile) |
|----------|-----------------|-----------------|
| Compared to | District's own past | Peer districts this week |
| "High" means | Unusual for this district | In top quartile right now |
| Is it comparable across weeks? | Yes — High in week 12 = High in week 30 | No — A1 in a quiet week ≠ A1 in peak season |
| Is it comparable across states? | Yes — Karnataka High = Odisha High | No — AP A1 ≠ Karnataka A1 |
| Best for | Outbreak detection, forecasting | Resource allocation, ranking |

Both are correct answers — to different questions. That is why the PRISM-H dashboard uses both.

---

## Part 11 — Which method does AP use?

From the PRISM-H specification:

- **AP Forecast tab (Risk Class)** → ICMR (A1/A2/A3/A4)
- **Karnataka and Odisha Forecast tab (Risk Class)** → WHO (Low/Moderate/High/Very High)
- **High Risk Areas tile (all three states)** → ICMR A1 + A2 AND trend is Rising

The map colours are the same (4 colours). Only the labels and the underlying method change.

---

## Part 12 — A third approach: the SOP weighted baseline

The High Risk Districts SOP describes yet another method, used for the operational weekly report.

The problem it is solving is different again:

> "I want a threshold that reflects both what is happening right now AND what usually happens at this time of year — not just one or the other."

The solution is a **weighted average**:

```
Baseline = 70% × (average of last 4 weeks)
         + 30% × (average of same weeks last year)
```

The 70% weight on recent weeks captures what is happening now. The 30% weight on same-season-last-year captures whether this is a seasonal pattern or a genuine anomaly.

Then the threshold (Upper Control Limit) is:

```
UCL = Baseline + n × SD

where SD = variation over the past 8 weeks
```

A district is **High Risk** if the predicted case count exceeds the UCL.

This is a 2-class output (High Risk / Normal), not 4-tier.

**Open question before we implement this:** the SOP has an inconsistency. Section 4.6 says use n = 2. Section 5 says use n = 3. These produce different thresholds. We need the team to confirm the correct value.

---

## Part 13 — Summary: three methods, three questions

| Method | Question | Output | Status |
|--------|----------|--------|--------|
| `historical` | Is this week unusual vs same-week history? | 4 zones: Low / Moderate / High / Very High | ✅ Implemented |
| `prev_nweeks` | Is this week unusual vs recent weeks? | 4 zones (fallback for cold-start) | ✅ Implemented |
| `icmr_quartile` | Where does this district rank among peers? | 4 strata: A1 / A2 / A3 / A4 | ✅ Implemented |
| `weighted_baseline` | Is this week above the weighted control limit? | 2 classes: High Risk / Normal | ⏳ Pending clarification |

---

## Part 14 — Where each method lives in the pipeline

The timing matters:

```
Step 1: generate_thresholds
   → Runs BEFORE model predictions
   → Computes: historical and prev_nweeks baselines
   → Why here? Because these only need historical case data

Step 2: train_and_predict
   → Runs model → gets predicted case counts for each district
   → Applies WHO zone classification by default
   → If configured for AP: overrides zones with ICMR quartile
   → Why here? Because ICMR needs to see ALL districts' predictions at once

Step 3: assess_thresholds
   → Looks at which WHO method (historical vs prev_nweeks)
     correctly identified high-risk districts in the past
   → Picks the better-performing method for this week's report
```

**ICMR cannot run in Step 1** because at that point the model has not yet produced predictions. ICMR needs to rank districts against each other, and those numbers do not exist until Step 2.

---

---

## Part 15 — How the SOP weighted baseline differs from the two WHO methods

You now know three threshold methods. Here is the key difference between them, put as simply as possible.

Each method has a different **memory**:

| Method | What it remembers | What it ignores |
|--------|------------------|-----------------|
| `prev_nweeks` | Last 4 weeks only | Everything before that |
| `historical` | Same week in past years | Recent weeks |
| SOP `weighted_baseline` | Both — but 70% recent, 30% seasonal | Nothing |

**Concrete example:**

It is July. Dengue season is starting. Kurnool had 15 cases/week through June (recent), and 40 cases/week last July (seasonal history).

- `prev_nweeks` baseline: ~15 (only sees June's quiet weeks → threshold too low, will over-alarm as July ramps up)
- `historical` baseline: ~40 (only sees last July → knows about the season, but doesn't know June was quiet)
- `weighted_baseline`: 0.7 × 15 + 0.3 × 40 = **22.5** (blends both → more stable, avoids extremes)

The weighted baseline is the most cautious. It won't sound an alarm just because the season is starting (that is expected), but it also doesn't ignore the recent trend entirely.

---

## Part 16 — Something new in the SOP: prediction intervals

The SOP introduces a concept not yet covered: **prediction intervals**.

The model does not predict one exact number. It predicts a range:

> "Kurnool this week: somewhere between 35 and 65 cases, most likely 50."

That range is the prediction interval (PI). It exists because:
- The model is not perfect
- Case data has reporting noise
- Weather and environmental factors add uncertainty

Why does this matter for thresholds?

Imagine the UCL is 48 and the predicted mean is 50. You would classify this as High Risk. But if the prediction interval is 35–65, you have low confidence the true value is above 48.

Now imagine the UCL is 48 and the predicted mean is 50, but the interval is 49–51. Very narrow. High confidence the true value is above 48. This is a more reliable High Risk call.

**The SOP says:** use prediction intervals as supporting evidence, especially when a prediction is close to the threshold. Do not over-rely on the point prediction alone.

We currently output point predictions. Adding prediction intervals is on the roadmap.

---

## Part 17 — Something entirely new: mandal-level disaggregation

The SOP section 8 describes something that has nothing to do with threshold methods. It is about **spatial scale**.

Our model predicts at the **district level**. But public health responders often need to know: **which mandal within the district should we focus on?**

The SOP's answer is a simple proportional split:

> Predicted mandal cases = District prediction × (mandal's share of recent cases)

Where "mandal's share" is:

```
mandal's 14-day moving average of cases
─────────────────────────────────────────────────────────
sum of all mandals' 14-day moving averages in the district
```

**Example:**

Kurnool district predicted: 50 cases this week.

| Mandal | 14-day avg | Share | Predicted cases |
|--------|-----------|-------|----------------|
| Kurnool Urban | 18 | 60% | 30 |
| Nandyal Rural | 6 | 20% | 10 |
| Panyam | 6 | 20% | 10 |

The district prediction (50) is split proportionally based on where cases have been occurring recently.

**Risk at mandal level** (SOP section 9): for now, a mandal simply inherits its district's risk label. If Kurnool district is A1 Critical, all its mandals are also A1 Critical. This is called "Homogeneous Inheritance" — it is a simplification, acknowledged as such in the SOP, with finer-grained mandal risk assessment planned for a future version.

This disaggregation work is tracked as a separate issue (issue #18).

---

## Open questions to resolve with the team

1. **SOP n_sigma: 2 or 3?** Section 4.6 says 2. Section 5 says 3. Need confirmation.
2. **SOP seasonal component:** "Same epi-weeks last year" — does this mean exact ISO week numbers, or exactly 52 weeks back? (They can differ by up to 6 days.)
3. **Is the SOP weighted baseline for the weekly operational report, and ICMR for the dashboard? Or do they overlap?** Knowing this determines whether `weighted_baseline` produces a separate output or replaces one of the existing methods.
4. **ICMR edge case:** what should the pipeline show when the total cases across all districts in the window is very low (e.g., < 10 total)? The PRISM-H spec says "Insufficient data" — not yet enforced.
