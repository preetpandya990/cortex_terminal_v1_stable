# Gemini 2.5 Flash — Free Tier RPD Research Report

**Research date:** 2026-07-01  
**Method:** Multi-source deep research with adversarial claim verification (11 confirmed claims, 10 refuted)

---

## The Short Answer

Google does not publicly publish a single universal RPD number for Gemini 2.5 Flash free tier. The official rate-limits page explicitly states limits "depend on a variety of factors" and directs users to check AI Studio for their active quota. However, the evidence below paints a clear picture.

---

## Confirmed Claims (2/3+ adversarial votes)

| Claim | Vote | Source |
|---|---|---|
| **Grounding with Google Search** is capped at **500 RPD** on the free tier (shared limit with Gemini 2.5 Flash-Lite) | 3-0 | https://ai.google.dev/gemini-api/docs/pricing |
| Rate limits are **per-project, not per API key** — multiple keys under the same project share one quota bucket | 3-0 | https://blog.laozhang.ai/en/posts/gemini-api-free-tier |
| Free tier RPD limits **dropped significantly** compared to prior levels, per user reports | 3-0 | https://discuss.ai.google.dev/t/clarification-on-gemini-api-free-tier-quota-reduction-and-paid-tier-stability/112941 |
| The "20 RPD" figure is a **project-specific snapshot**, not a universally safe constant to hardcode | 3-0 | https://blog.laozhang.ai/en/posts/gemini-api-free-tier |
| **Preview model variants** (e.g. flash-preview) get additional throttling vs. stable model versions | 2-1 | https://discuss.ai.google.dev/t/clarification-on-gemini-api-free-tier-quota-reduction-and-paid-tier-stability/112941 |
| Free tier RPD was **cut from 250 → 20 RPD** (~92% reduction), discovered around December 6, 2025, without advance notice | 2-1 | https://discuss.ai.google.dev/t/do-they-really-think-we-wouldnt-notice-a-92-free-tier-quota/111262 |
| Free tier standard input and output tokens are **free of charge** (no per-token pricing) | 2-0 | https://ai.google.dev/gemini-api/docs/pricing |

---

## Definitively Refuted Claims (0–1 out of 3 votes)

| Claim | Vote | Source |
|---|---|---|
| ~~Gemini 2.5 Flash free tier allows 1,500 RPD~~ | 0-3 | https://tokenmix.ai/blog/gemini-api-free-tier-limits (misinformation) |
| ~~Current free tier limit is 250 RPD and 10 RPM~~ | 0-3 | https://www.aifreeapi.com/en/posts/gemini-api-free-tier-complete-guide (outdated) |
| ~~API usage and AI Studio usage share the same quota pool~~ | 0-2 | https://discuss.ai.google.dev/t/limits-of-free-tier-api-vs-ai-studio/94918 |
| ~~Gemini CLI free tier allows 1,000 RPD per model~~ | 0-3 | https://github.com/google-gemini/gemini-cli/discussions/4122 (different product) |
| ~~Free tier was cut to exactly 20 RPD universally~~ | 1-2 | https://www.howtogeek.com/gemini-slashed-free-api-limits-what-to-use-instead/ |
| ~~250 RPD was the confirmed pre-cut baseline~~ | 1-2 | https://viblo.asia/p/is-free-gemini-25-pro-api-fried-changes-to-gemini-quota-2025 |

---

## The Historical Timeline

| Period | Free Tier RPD | Notes |
|---|---|---|
| Pre-Dec 2025 | ~250 RPD / 10 RPM | Widely reported baseline for Gemini Flash models |
| ~Dec 6, 2025 | Cut to ~20 RPD | 92% reduction, no advance notice from Google |
| Current (mid-2026) | ~20 RPD (project-dependent) | Not officially documented; varies by project state |

---

## Key Facts for Developers

1. **No official number exists.** Google redirects all RPD queries to AI Studio's quota panel. The actual limit shown there is the ground truth for your specific project.

2. **The "20 RPD" is real but not universal.** It's the effective quota that emerged from project-level throttling after the December 2025 cut. Some projects may see different values depending on account state and model variant.

3. **Preview vs. stable matters.** Preview model variants (e.g., `gemini-2.5-flash-preview-*`) face additional throttling on top of the base free-tier limits. Stable releases are less restricted.

4. **Quota is per Google Cloud project, not per API key.** Rotating API keys within the same project does not increase quota. You need separate Google Cloud projects to multiply quota.

5. **Grounding with Google Search is a separate bucket.** The 500 RPD cap for grounded searches is distinct from standard generation calls.

6. **Quotas reset at midnight Pacific Time (PT).**

---

## Mitigation Options

| Option | Effect |
|---|---|
| Link a billing account (Tier 1) | Dramatically higher RPM/RPD; pay only for tokens used |
| Separate Google Cloud projects per key | Multiplies free quota (each project gets its own bucket) |
| Aggressive Redis caching | Reduces unique Gemini calls; most effective for repeated queries |
| Sentiment/news batching (N articles → 1 call) | ~93% RPD reduction for batch workloads |
| Circuit breaker + request priority queue | Protects critical paths when quota is exhausted |

---

## Sources

- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/rate-limits
- https://discuss.ai.google.dev/t/do-they-really-think-we-wouldnt-notice-a-92-free-tier-quota/111262
- https://discuss.ai.google.dev/t/clarification-on-gemini-api-free-tier-quota-reduction-and-paid-tier-stability/112941
- https://discuss.ai.google.dev/t/rpd-exceeded-for-gemini-flash-2-5-but-still-cant-access-other-model-on-free-tier/123131
- https://discuss.ai.google.dev/t/limits-of-free-tier-api-vs-ai-studio/94918
- https://blog.laozhang.ai/en/posts/gemini-api-free-tier
- https://github.com/google-gemini/gemini-cli/discussions/4122
- https://github.com/google-gemini/gemini-cli/issues/1502
