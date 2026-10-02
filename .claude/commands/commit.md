Create a git commit for the current changes in the RealEstateMagnet project.

Steps:
1. Run `git status` and `git diff --stat` to understand what changed.
2. Run `python3 -m pytest tests/ -q` — do NOT commit if tests are failing unless the user explicitly says to skip tests.
3. Stage the appropriate files. Prefer specific file paths over `git add -A`. Never stage:
   - `.env` (secrets)
   - `data/catalogs/*.json` (generated output, in .gitignore)
   - `transcripts/` (generated, in .gitignore)
   - `__pycache__/` or `.pytest_cache/`
4. Write a commit message following this project's convention:
   - First line: imperative mood, ≤ 72 chars, phase prefix where applicable
     e.g. `phase1: add web crawler with Playwright support`
     e.g. `infra: add devcontainer and Proxmox installation scripts`
   - Body (if needed): what changed and why, not a file list
5. Commit with:
   ```bash
   git commit -m "$(cat <<'EOF'
   <message>

   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
   EOF
   )"
   ```
6. Report the commit hash and summary.

Phase prefixes: `phase1:`, `phase2:`, `phase3:`, `phase4:`, `phase5:`, `infra:`, `docs:`, `tests:`, `fix:`, `chore:`
