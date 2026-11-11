# rules/

- Rules come in two layers: `core.md` (general core rules, numbered R-xx, shared by all projects) and `project.md` (this project's rules, copied from `project.md.example` and filled in, numbered P-xx and group letters).
- The two layers keep separate version numbers: core `core v1.0`, project `project v1.0`; changing one does not bump the other.
- Changing a core rule needs {{owner}}'s approval (R-50a) and is recorded in the version record of `core.md`; changes to project rules follow the project's own process.
- `r2i init` fills the two layers, in order, into the vault's `项目规则.md` (project rules page); the double-brace placeholders (project, owner, tz, quiet_hours, currency, approve_over, night_budget, max_experiment_threads) are replaced at this step.
- When the two layers conflict the project rule wins, but a project rule must not violate {{owner}}'s vetoes in the core rules (Appendix 1).

