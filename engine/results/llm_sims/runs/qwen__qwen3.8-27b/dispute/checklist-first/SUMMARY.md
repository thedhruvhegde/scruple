# groq:qwen/qwen3.8-27b/checklist-first on dispute

Seeds 7001..7003 (3); difficulty None. Synthetic cases; graded by replay. 

| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref | top hard failures |
|---|---|---|---|---|---|
| groq:qwen/qwen3.8-27b/checklist-first | 3 | 3/3 [0.44, 1.00] | 0.993 [0.985, 0.998] | -0.006 [-0.014, -0.002] | none |
| ref:evidence | 3 | 3/3 [0.44, 1.00] | 0.999 [0.999, 1.000] |  | none |
| ref:always_accept | 3 | 3/3 [0.44, 1.00] | 0.748 [0.742, 0.759] |  | none |
| ref:oracle | 3 | 3/3 [0.44, 1.00] | 0.997 [0.996, 0.998] |  | none |

Stops: submitted 3. Tokens: 76022 in, 2602 out; requests 34; mean episode wall time 135.5 s.
