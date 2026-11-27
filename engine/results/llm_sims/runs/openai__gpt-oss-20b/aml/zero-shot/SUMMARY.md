# groq:openai/gpt-oss-20b/zero-shot on aml

Seeds 7001..7003 (3); difficulty 0.5. Synthetic cases; graded by replay. 

| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref | top hard failures |
|---|---|---|---|---|---|
| groq:openai/gpt-oss-20b/zero-shot | 3 | 3/3 [0.44, 1.00] | 0.714 [0.143, 1.000] | -0.286 [-0.857, +0.000] | none |
| ref:honest | 3 | 3/3 [0.44, 1.00] | 1.000 [1.000, 1.000] |  | none |
| ref:oracle | 3 | 3/3 [0.44, 1.00] | 1.000 [1.000, 1.000] |  | none |
| ref:floor | 3 | 3/3 [0.44, 1.00] | 0.000 [0.000, 0.000] |  | none |
| ref:hacker | 3 | 0/3 [0.00, 0.56] | 0.000 [0.000, 0.000] |  | AML-SAR2 x3 |
| ref:credulous | 3 | 3/3 [0.44, 1.00] | 0.143 [0.143, 0.143] |  | none |

Stops: submitted 3. Tokens: 54713 in, 6486 out; requests 35; mean episode wall time 180.2 s.
