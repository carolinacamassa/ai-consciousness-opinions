# AI Consciousness Opinions

What people on X say about whether AI systems are conscious, and about whether they are owed moral consideration
(including arguments for or against that don't depend on consciousness), sorted into three stances: **affirming**,
**skeptical** and **uncertain**. The tweets come from the [Community Archive](https://www.community-archive.org), an
open, opt-in archive of X, so the site describes what the accounts in that archive say, not public opinion.

The code is adapted from [Model Behavior Reports](https://github.com/aVariengien/model-behavior-reports) by
Alexandre Variengien, which does the same thing for reports of how LLMs behave. The fetching, context, ranking,
summaries and static build are his; the searches, the grading prompt, the stance-based pages and the per-author
cap on summaries are new.

## How it works

`aco update` does, in order:

1. **Searches** the archive's full-text index for tweets inserted or updated since the last run, using the terms in
   `aco/config.py` (`SEARCHES`): conscious, sentient, qualia, model welfare, moral patient, AI rights and a few more.
   Only original tweets and quote tweets become opinions. Replies to other people are dropped as soon as they are
   found and never fetched, graded or counted. A tweet that continues its author's own thread stands for the thread:
   the thread's first tweet becomes the candidate, and the whole thread is one opinion.
2. **Hydrates** each candidate with its quoted tweet, the author's thread under it, its top 10 replies (shown for
   context only) and media, and looks up playable video URLs and missing avatars from X's public embed endpoint
   (`aco/xmedia.py`).
3. **Grades** each match with Gemini 3.8 Flash (text and attached images, `aco/classify.py`): is it a human opinion
   on AI consciousness or moral status, which stance it takes, what kind of tweet it is (argument, opinion,
   question, news, model output, joke, off topic), how much substance it has (1-10), and the author's point in a
   few words. Tweets that only relay what a model said about itself are left out. About $0.0012 per tweet.
4. **Tags arguments**: each shown opinion gets up to three arguments from the fixed, versioned list in
   `aco/arguments.json` (48 arguments in 8 families), with Gemini 3.8 Flash (`aco/arguments.py`, about $0.004 per
   opinion). Bumping the list's `version` re-tags everything. Flash was picked against Claude Opus 5.5 as a reference
   on 100 opinions: F1 0.79, the best of the setups tried.
5. **Counts quotes**: distinct archive accounts that quote-tweeted each opinion.
6. **Summarizes** each stance with Claude Opus 5.5, at most weekly and only when new opinions arrived, from the
   stance's strongest opinions with at most four per account (`aco/summarize.py`). Each summary costs about $0.45.
7. **Builds** a static site in `dist/` plus plain-text exports for LLMs (`/llms.txt`, `/llms-full.txt`,
   `/data/<stance>.md`).

Opinions are shown when the grader gives them a substance of 4 or more, and ranked by
`50 × quotes + 20 × log2(1 + min(retweets, 100)) + 15 × substance`. Add `?debug=1` to any page to see each
tweet's grade.

## Running it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). All LLM calls go through
[OpenRouter](https://openrouter.ai).

```bash
uv sync
export OPENROUTER_API_KEY=...
uv run aco backfill               # run the searches over every tweet since START_DATE (free, ~15 minutes)
uv run aco classify              # hydrate and grade the matches
uv run aco quotes && uv run aco videos
uv run aco summarize
uv run aco build                 # static site in dist/
uv run aco update                # the scheduled job: all of the above, incrementally
uv run aco stats
```

To serve the site under a path rather than a domain root (a GitHub Pages project site, for example), set
`ACO_BASE=/ai-consciousness-opinions/` before `build`. `ACO_SITE_URL` sets the absolute URL used in `llms.txt`.

Data lives in `data/aco.sqlite`, which is not committed. The start date (`START_DATE`, June 1, 2026), the stance
names and definitions, the searches, the score threshold and the ranking weights are at the top of `aco/config.py`.
Moving `START_DATE` earlier makes the next `aco update` search the extra days.

## Hosting on GitHub Pages

No server is needed. `.github/workflows/update.yml` runs every 3 hours, and on demand from the Actions tab. It
restores the database from the `data` branch, runs `aco update`, saves the database back to `data` (one gzipped
file that each run overwrites, so the branch's history doesn't grow), and publishes `dist/` to GitHub Pages at
`https://<owner>.github.io/<repo>/`. If it can't restore the database it stops instead of publishing an empty site.

One-time setup:

1. Make `main` the default branch (Settings > General). Scheduled workflows only run on the default branch, and
   Pages only deploys from it.
2. Settings > Pages > Build and deployment > Source: **GitHub Actions**.
3. Settings > Secrets and variables > Actions > New repository secret: `OPENROUTER_API_KEY`. A dedicated OpenRouter
   key with a credit limit is safest.
4. If there is no `data` branch yet, run the workflow once from the Actions tab with `fresh_start` ticked. It
   searches from `START_DATE` and grades the backlog 400 candidates per run.

The `data` branch is as public as the repository. Before saving, each run deletes the tweets the site neither shows
nor uses as context (`aco prune`), keeping only their grades so they aren't graded again.

## Data and credits

Tweets come from the [Community Archive](https://www.community-archive.org) through its public API. Built on
[Model Behavior Reports](https://github.com/aVariengien/model-behavior-reports) by Alexandre Variengien. MIT licensed.
