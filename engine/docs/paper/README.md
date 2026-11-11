# paper

- `scruple_paper.pdf`: the paper, preprint, november 2026 (built from `scruple_paper.tex`)
- `scruple_paper.tex`, `scruple_paper.bib`, `betting_results.tex`: source; every proof is in the text
- build: `pdflatex scruple_paper && bibtex scruple_paper && pdflatex scruple_paper && pdflatex scruple_paper`
- diagrams (system, dispute lifecycle, verifier stack) are tikz, drawn from the code and `docs/RULES.md`
- plots are pgfplots, numbers pasted from the result files named in each caption
- appendix a maps every section to its result files and commands
- longer notes and raw script output per result: `docs/math/01` to `09`
