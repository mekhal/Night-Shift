# Image generation prompts

## ai-review-loop.png

Generated with the built-in image_gen tool for the README section "The loop between the AIs".

Use case: infographic-diagram. Asset: clear English explanatory image for the Night-Shift README section "The loop between the AIs". Create a polished wide landscape infographic, white background, navy headings, teal automated steps, purple fix loop and amber human intervention, generous whitespace, crisp large typography and simple flat icons. Prioritize immediate comprehension, no spaghetti arrows, no fake UI. Title "The loop between the AIs". Subtitle "Implement → check → review → improve".
Main row of FOUR large boxes left to right with teal arrows: "1. Pick approved task" (subtitle "Dependencies ready + quota available"), "2. Write tests + code" (subtitle "GPT-5.5 → Claude Sonnet fallback"), "3. Run gates" (subtitle "Scope, tests and quality checks"), "4. AI review" (two subtitles "Claude Opus — primary" and "+ GPT-6-Astra for joint tasks"). Gate-to-review arrow label "Pass".
Under boxes 2–4, one clear purple return loop from AI review back to Write tests + code labeled "Changes requested → fix → run gates again". A small purple callout under Run gates reads "Gate failure: one fix, then human". Use a SHORT purple branch from Run gates into the return loop, avoiding crossing other arrows.
Below that, two separated outcome cards: LEFT teal "Approved" with simple inline sequence "Merge to develop → Recheck gates → Publish to GitHub". Under it smaller text "Post-merge failure: revert + stop for human". Connect AI review to Approved using a teal arrow routed around the right outer margin with no line crossing the return loop; if this makes routing crowded, use a matching teal outcome marker beside AI review and Approved instead.
RIGHT amber "Needs human" with three bullet labels "Retry limit reached", "AI cannot decide", "Reviewers disagree". Do not give this card any outgoing arrow that implies automatic approval.
Footer "Reviewers read and give feedback. Implementers write and fix." Add concise small note "Up to 2 review fix rounds per cycle. Release to main stays human-only."
All content is a simplified explanation; detailed timing and failure limits remain in the caption. No extra labels or brand logos. Text must be legible at GitHub README width.

## workflow-v3.png

Generated with the built-in image_gen tool by revising an earlier draft. This is the current README workflow image; unused drafts have been removed.

Edit the supplied Night-Shift workflow infographic. Preserve layout, arrows, icons and all other content. Make only these text changes: replace subtitle "Intended model roles" with "Model roles"; in the Tests + code box replace "ChatGPT 5 or Claude Sonnet" with "GPT-5.5 or Claude Sonnet"; in AI review box replace the model line with two clearly readable lines "Claude Opus (primary)" and "+ GPT-6-Astra for joint tasks". Keep AI planning subtitle "Claude Opus + ChatGPT 6 astra" unchanged. Keep human requirements, human approval, automated gates, bounded fix loop and human decision escalation intact. Match existing typography and colors, enlarging the review box text area slightly only if needed.

## architecture.png

Use case: infographic-diagram. Create a polished landscape architecture infographic for Night-Shift README. White background, navy text, teal automation and amber human-only release. Title "Where Night-Shift runs". Three clearly separated panels arranged left to right: "WSL" with boxes "Supervisor", "Coding agents + reviewers", "Task worktrees", "Local develop"; "Windows" with boxes "Task Scheduler", "GitHub sync job", "Windows clone"; "GitHub" with boxes "develop" and "main". Main publishing path ONLY: WSL Local develop -> Windows GitHub sync job / Windows clone labeled "git bundle"; Windows clone -> GitHub develop labeled "Push every 30 min"; GitHub develop -> main labeled "Human release" in amber. WSL footnote "No GitHub credentials". Windows Task Scheduler connects to WSL Supervisor with a clearly distinct thin arrow labeled "Start loop". Keep lines clean, avoid crossings and redundant connections. Crisp readable English typography, tasteful simple computer and repository icons, technically accurate flat infographic. Wide 16:9.

## ai-dlc-comparison.png

Use case: infographic-diagram. Create a polished wide English infographic comparing AI-DLC and Night-Shift for a GitHub README. White background, navy text, teal AI actions and amber human decisions, clean flat icons and generous readable typography. Title "From AI-DLC to Night-Shift". Two horizontal swimlanes. Upper lane title "AI-DLC", boxes left to right "Plan" -> "Human approval" -> "AI implementation" -> "Human review" -> "Merge". Lower lane title "Night-Shift", boxes left to right "Human-approved tasks" -> "Tests + code" -> "Automated gates" -> "AI review" -> "Merge to develop". From lower lane AI review a branch down to amber "Human escalation" labeled "When unresolved". Bottom caption verbatim "Releases to main remain human-only". Do not imply humans never participate in Night-Shift. No model names, metrics, brand logos or fake UI. Clear arrows, flat vector-like illustration rendered as a high quality raster. Wide 16:9.
