# Golden Dataset Quality Issues

Source: `/Users/aleksejcuprynin/Desktop/AgentOC/ag infra up/golden_dataset.json`

## Summary
- Cases with no outbound reply: 34
- HTML-noise cases: 4 (`304`, `337`, `343`, `347`)
- Marketing/spam-like cases: 2 (`281`, `424`)
- Empty-body cases: 1 (`666`)
- `reply_situation` mismatch: 1 (`471`: `other -> tracking`)

## IDs by issue
- `no_outbound`: `64, 66, 82, 84, 98, 111, 122, 133, 148, 159, 172, 201, 281, 286, 296, 300, 302, 304, 317, 319, 321, 325, 337, 343, 346, 347, 389, 402, 424, 437, 569, 603, 666, 871`
- `html_noise`: `304, 337, 343, 347`
- `marketing_spam`: `281, 424`
- `empty_body`: `666`
- `reply_situation_mismatch`: `471`

## Recommended handling in eval
1. Keep all 132 for routing robustness checks.
2. Exclude `no_outbound` cases from strict generation similarity scoring.
3. Track `html_noise`, `marketing_spam`, `empty_body` in a separate robustness bucket.
4. Treat case `471` as route anomaly candidate; verify expected label manually before using it as strict ground truth.
