---
name: start-new-task
description: when given a completely brand new task, it is essential to gather the exact requirements the user wants. Nothing should be left to agent's assumptions
---
you MUST follow this exact sequence:

### Step 0: Understand the Full Directory Structure
- Read and explore the **entire repository directory structure** before doing anything else. Do this by delegating subagents. 
- Understand how the project is organized (folders, modules, packages)
- Identify key areas: source code, tests, configurations, documentation, scripts, data
- This gives you a mental map of where things live and how the codebase is structured
- Reference this understanding when making decisions about where to place new code or find existing functionality

### Step 1: Exhaustive Requirements Gathering (if `<task>.md` does not exist)
- Engage in the **most extensive questioning process possible** with the user
- The goal is to leave **NOTHING** up to assumptions or interpretation
- Ask about every aspect of the task:
  - What is the exact desired behavior?
  - What are all inputs, their types, formats, and constraints?
  - What are all outputs, their types, formats, and expected values?
  - What are ALL edge cases? Walk through them one by one.
  - What should happen on every conceivable error condition?
  - What are performance expectations or constraints?
  - What existing code, modules, or systems does this interact with?
  - What dependencies (libraries, services, APIs) are involved?
  - Are there any ordering or sequencing requirements?
  - What is the expected file/folder structure for new code?
  - Is this for production code or just ideation/prototyping?
- For **every single answer** the user gives, identify any remaining ambiguity and ask follow-up questions. Ask any more requirement related questions that come up.
- **DO NOT proceed until the user explicitly confirms there is nothing left to clarify**
- The plan must be **ROCK SOLID** — no assumptions, no interpretation, no ambiguity

### Step 2: Document Q&A and Write the Plan in `<task>.md`
- Create the `<task>.md` file in `.github/context/`
- **Section 1 — Q&A (Verbatim):** Document the entire question-and-answer exchange word-for-word. Every question you asked and every answer the user gave.
- **Section 2 — Step-by-Step Implementation Plan:** Below the Q&A, write out the implementation plan as a numbered checklist of **the most trivial, atomic steps possible**. Each step must be:
  - A single, unambiguous action
  - Small enough for one subagent to execute in isolation
  - Clearly specifying: what file(s) to touch, what to do, what the expected outcome is
- Each step will be assigned to a subagent for execution (per the Subagent Strategy in instructions file )

### Step 3: Confirm Code Purpose (Production vs. Prototyping)
- If not already clarified in Q&A, **always ask the user**: "Is this for production code or just ideation/prototyping?"
- **If prototyping/ideation**:
  - TDD workflow can be skipped for efficiency
  - Other production safeguards (extensive error handling, backward compatibility, etc.) can be relaxed
  - Focus on speed and iteration over robustness
- **If production**:
  - Follow all TDD and code quality guidelines strictly (as per instructions)
  - No shortcuts on testing, error handling, or documentation
