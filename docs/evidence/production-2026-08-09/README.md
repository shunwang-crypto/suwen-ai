# Production browser acceptance — 2026-08-09

## Environment and scope

- Web deployment: `https://20-239-104-201.sslip.io`
- Browser: Playwright Chromium in WSL, desktop `1440 × 1024` and mobile `390 × 844`
- Authenticated role: platform administrator
- Evidence contains no password, access token, or server secret.
- Production state created during the run:
  - knowledge base `线上验收·二次函数·20260809`
  - document `online-acceptance-quadratic-functions.md`
  - one RAG chat and one tutor session
  - one verified manual error item and one completed review

## Outcome

The deployment, authentication, responsive shell, knowledge-base ingestion, hybrid retrieval, cited RAG answer, process timeline, and two-level Socratic tutor interaction worked. The verified-error → review queue → answer grading → FSRS reschedule path also worked.

The smart-quiz path is a release blocker. Blueprint generation and human confirmation both succeeded, and five knowledge-base materials reached the generator. The formal question generator returned zero questions for three consecutive validation rounds. The graph then emitted an empty `done` result. The UI incorrectly rendered that state as `已取消出题（蓝图未确认）`, even though the SSE trace records `confirm_blueprint: approved`.

Additional findings:

1. A logged-out visit calls `/api/auth/refresh`, receives an expected `401`, and logs it as a browser console error. The page still works, but production monitoring will contain avoidable noise.
2. A manually entered error item requests `/api/errors/imports/{id}/source` and receives `404`, even though manual entry has no uploaded source file.
3. A completed review updated the error distribution and FSRS state, but did not create mastery evidence because the manually entered knowledge-point name was not resolved to a knowledge-concept ID. The profile still showed `0` knowledge points.
4. The first automated RAG text assertion missed `(2,-1)` because KaTeX inserts spaces into rendered DOM text. Visual and DOM review confirmed the answer itself was correct: vertex `(2,-1)`, roots `x=1` and `x=3`, with five citations.

## Evidence map

- `screenshots/`: all primary routes plus desktop/mobile shell review.
- `full-flow/`: knowledge-base creation/upload, ingestion, retrieval, RAG, tutor, quiz blueprint editing, and the first quiz failure.
- `full-flow-retry/`: unedited-blueprint retry and raw SSE trace proving three empty generations and validation failures.
- `learning-loop/`: verified manual error entry, real review submission, FSRS result, profile/report/today checks, and before/after billing.
- `input/`: the exact Markdown document uploaded during acceptance.

Machine-readable details are in each folder's `*-results.json`. Browser credentials are supplied only through environment variables when the scripts run.
