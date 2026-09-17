# Empty values and normalization

Scalar field mappings accept `emptyValuePolicy: "omit"` to leave destination properties untouched when source values are null, empty strings, or whitespace. The default `"preserve"` keeps existing behavior: null is omitted and empty strings are written. Zero and false remain values. Required blanks reject under omit, including blanks produced by a transform or value map.

`normalize_email` trims and lowercases the entire email. This is an explicit normalization rule, not a validity or deliverability check. `normalize_domain` accepts a hostname or HTTP(S) URL, extracts and lowercases the hostname, converts IDNs to ASCII and removes a trailing dot. Paths, queries and ports are discarded; credentials, malformed hosts and unsupported schemes reject.

Both the policy and transform survive plan serialization and affect the plan hash. Hosted consumers must upgrade to a published runtime containing these features before enabling them. No provider implementation or provider credentials are included in this package.
