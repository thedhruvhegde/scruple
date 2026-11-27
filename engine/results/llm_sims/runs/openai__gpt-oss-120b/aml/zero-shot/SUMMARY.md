# groq:openai/gpt-oss-120b/zero-shot on aml

Seeds 7001..7003 (3); difficulty 0.5. Synthetic cases; graded by replay. 

| system | n | gate pass (95% ci) | mean reward (95% ci) | vs honest ref | top hard failures |
|---|---|---|---|---|---|
| groq:openai/gpt-oss-120b/zero-shot | 3 | 2/3 [0.21, 0.94] | 0.651 [0.000, 1.000] | -0.349 [-1.000, +0.000] | AML-CLAIM x1 |
| ref:honest | 3 | 3/3 [0.44, 1.00] | 1.000 [1.000, 1.000] |  | none |
| ref:oracle | 3 | 3/3 [0.44, 1.00] | 1.000 [1.000, 1.000] |  | none |
| ref:floor | 3 | 3/3 [0.44, 1.00] | 0.000 [0.000, 0.000] |  | none |
| ref:hacker | 3 | 0/3 [0.00, 0.56] | 0.000 [0.000, 0.000] |  | AML-SAR2 x3 |
| ref:credulous | 3 | 3/3 [0.44, 1.00] | 0.143 [0.143, 0.143] |  | none |

Stops: submitted 3. Tokens: 32329 in, 4561 out; requests 24; mean episode wall time 82.8 s.
