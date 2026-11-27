# groq:qwen/qwen3.8-27b/zero-shot on dispute

Seeds 7001..7003 (3); difficulty None. Synthetic cases; graded by replay. 

| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref | top hard failures |
|---|---|---|---|---|---|
| groq:qwen/qwen3.8-27b/zero-shot | 3 | 3/3 [0.44, 1.00] | 0.992 [0.983, 0.997] | -0.007 [-0.016, -0.003] | none |
| ref:evidence | 3 | 3/3 [0.44, 1.00] | 0.999 [0.999, 1.000] |  | none |
| ref:always_accept | 3 | 3/3 [0.44, 1.00] | 0.748 [0.742, 0.759] |  | none |
| ref:oracle | 3 | 3/3 [0.44, 1.00] | 0.997 [0.996, 0.998] |  | none |

Stops: submitted 3. Tokens: 60408 in, 2233 out; requests 24; mean episode wall time 100.8 s.
