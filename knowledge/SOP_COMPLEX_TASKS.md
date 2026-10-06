# SOP: Complex Tasks
Golden Rule: 3+ steps -> UTS. One script, one bash run, one log check. No fragmented tool chains.
UTS: 1) write ./task_<ts>/script.py (template: read_file ../knowledge/uts_template.py) 2) run via bash in task dir 3) check logs/execution.log; on error: fix+rerun, never re-create.
Rules: isolate per task_<ts>/; atomic .tmp->move; edit existing files only via tools (auto-backup); verify syntax+import+content; print final JSON {status,...}.
