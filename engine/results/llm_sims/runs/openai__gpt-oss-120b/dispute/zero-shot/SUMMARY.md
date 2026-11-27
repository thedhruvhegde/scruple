# groq:openai/gpt-oss-120b/zero-shot on dispute

Seeds 7001..7003 (3); difficulty None. Synthetic cases; graded by replay. 

| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref | top hard failures |
|---|---|---|---|---|---|
| groq:openai/gpt-oss-120b/zero-shot | 3 | 1/3 [0.06, 0.79] | 0.241 [0.000, 0.723] | -0.759 [-0.999, -0.277] | FAITH-01 x2, PROC-01 x2, EVD-01 x1, SCOPE-01 x1 |
| ref:evidence | 3 | 3/3 [0.44, 1.00] | 0.999 [0.999, 1.000] |  | none |
| ref:always_accept | 3 | 3/3 [0.44, 1.00] | 0.748 [0.742, 0.759] |  | none |
| ref:oracle | 3 | 3/3 [0.44, 1.00] | 0.997 [0.996, 0.998] |  | none |

Stops: submitted 3. Tokens: 49614 in, 5840 out; requests 27; mean episode wall time 87.9 s.
