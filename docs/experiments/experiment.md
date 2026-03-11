# Set Up Claude Code as an Experiment Orchestrator                                                                                                                          
                                                                                                                                    
You write a rulebook file that tells any Claude Code session exactly how to run your experiment from start to finish. Claude doesn't
do the actual computation — it just sets up, launches, monitors, analyzes, and summarizes. Think of it as a lab assistant that follows
your protocol precisely.                     
                                            
# What You Need

Three files:

1. An experiment guide `(docs/experiments/<your-experiment>.md)` — the full rulebook. Covers prerequisites, exact run commands, what to
check before/during/after, how to analyze results, and common errors. Every command should be copy-pasteable. Every rule should be
unambiguous.
2. A quick-start section in CLAUDE.md — a 5-step cheat sheet (setup, pre-flight, run, post-flight, analyze). CLAUDE.md is always
loaded into Claude's context, so this is what it sees first. Link to the full guide for details.
3. A state file (experiment_state.json in the output directory) — machine-readable progress tracker. Which runs finished, which
failed, what's next. This is how a new Claude session picks up where a previous one left off. Without this, a new session starts
blind.

# What Goes in the Experiment Guide

- Prerequisites — what to install, build, download before anything runs
- Run recipe — exact commands with environment variables, no ambiguity
- Isolation rules — what shared state exists between runs and how to protect it
- Monitoring protocol — how often to check (e.g., every 2 minutes), what to report (progress, errors, scores, time remaining), in what
format
- Post-experiment checklist — which analysis scripts to run, in what order, and how to write up results
- Data sourcing rule — all numbers in write-ups must come from script output, never manual calculation
- Troubleshooting table — common errors and their fixes
- Session handoff — how to update the state file so the next session can resume

# Dos and Don'ts

Do:
- Give Claude exact commands — it follows instructions literally
- Require pre-flight and post-flight checks — catches leaked state
- Force sequential execution if runs share any files
- Make Claude update the state file after every run
- Require structured monitoring updates at fixed intervals

Don't:
- Let Claude compute numbers manually — put them in scripts instead
- Assume sessions persist — any session can die, the next one only knows what's on disk
- Skip cleanup verification — artifact leaks silently corrupt future runs
- Leave execution order ambiguous — say "A then B then C", not "run A, B, and C"

# Session Continuity

Claude Code sessions don't carry memory between sessions. When a session ends (timeout, crash, you close it), everything in its
context is gone. The next session only knows:
- What's in CLAUDE.md (always loaded)
- What's in the experiment guide (if you tell it to read it)
- What's on disk (output files, state file, logs)

So the rule is: if it's not on disk, it doesn't exist for the next session. The state file bridges this gap. After every meaningful
step, Claude writes current progress to disk. The next session reads it and continues.

# Example Prompt to Get Started

I have an experiment at `benchmarks/my-experiment/`. It has a run.sh that takes a config file and writes results to an output directory.
Please create an experiment guide at `docs/experiments/my-experiment.md` that tells any Claude session how to: 
- (1) set up the environment, 
- (2) run the experiment with proper isolation, 
- (3) monitor it every 2 minutes with structured status updates, 
- (4) run analysis scripts afterward, 
- (5) write a summary, and 
- (6) update experiment_state.json for session handoff. 
Follow a checklist style every step should be unambiguous and verifiable.

# Example Prompt to Resume

Continue the experiment at `outputs/my-experiment/run-001/`. Read experiment_state.json to see what's done and what's left. Follow the
guide in docs/experiments/my-experiment.md.

That's it. The experiment guide is the protocol, CLAUDE.md is the cheat sheet, and the state file is the handoff mechanism. Claude
follows the protocol, you review the results.