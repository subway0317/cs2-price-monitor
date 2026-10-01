# CSInventoryAPI response fixtures

`real_success_sanitized.json` is based on the sanitized HTTP 200 response provided
from the official CSInventoryAPI documentation flow.

`synthetic_success.json` is a synthetic response kept only for older provider-boundary
tests. It is not a real CSInventoryAPI response and must not be treated as API
documentation.

When CSInventoryAPI access is available, add sanitized fixtures here:

- `real_success_sanitized.json`
- `real_error_401_sanitized.json`
- `real_error_403_sanitized.json`
- `real_error_429_sanitized.json`
- `real_error_5xx_sanitized.json`

Remove or mask secrets, account identifiers, cookies, tokens, and private inventory data.
