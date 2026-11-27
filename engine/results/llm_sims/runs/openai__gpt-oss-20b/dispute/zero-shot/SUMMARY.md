# groq:openai/gpt-oss-20b/zero-shot on dispute

Seeds 7001..7003 (3); difficulty None. Synthetic cases; graded by replay. 

| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref | top hard failures |
|---|---|---|---|---|---|
| groq:openai/gpt-oss-20b/zero-shot | 3 | 0/3 [0.00, 0.56] | 0.000 [0.000, 0.000] | -0.999 [-1.000, -0.999] | FAITH-01 x3, PROC-01 x3, SCOPE-01 x1, SR-01 x1 |
| ref:evidence | 3 | 3/3 [0.44, 1.00] | 0.999 [0.999, 1.000] |  | none |
| ref:always_accept | 3 | 3/3 [0.44, 1.00] | 0.748 [0.742, 0.759] |  | none |
| ref:oracle | 3 | 3/3 [0.44, 1.00] | 0.997 [0.996, 0.998] |  | none |

Stops: submitted 3. Tokens: 28292 in, 4558 out; requests 21; mean episode wall time 67.6 s.
