# Agent prompts — yours to write

Each agent's personality, role, and behaviour lives in its own file here:

| File | Agent | Status |
|---|---|---|
| `EV.prompt.md` | E.V., central intelligence | **Starter**: her previous built-in instructions, moved here unchanged |
| `FRIDAY.prompt.md` | F.R.I.D.A.Y., market intelligence | **Temporary**: a minimal placeholder for development |
| `TEMPLATE.prompt.md` | — | Fill-in-the-blanks structure to copy |

**To edit:** open a file in any text editor, save it, and send E.V. a new message. Files are re-read automatically, so you don't need to restart. The **Agents** page shows each file's status (Temporary / Starter / Yours).

**Status line:** the first line, `<!-- EV-PROMPT-STATUS: TEMPORARY -->`, marks a placeholder. When the prompt is yours, delete that line or change it to `CUSTOM`.

**What these files can't change.** The following are enforced in code, independent of anything written here:

- tool lists
- permission levels and approval requirements
- memory access
- step limits, timeouts, and delegation limits
- the specialist report format

E.V.'s code appends those rules after your text. They live in `backend/services/agents.py`.

Comments (`<!-- … -->`) are stripped before the prompt is sent to the AI, so you can leave notes to yourself. Each file is capped at 20,000 characters.
