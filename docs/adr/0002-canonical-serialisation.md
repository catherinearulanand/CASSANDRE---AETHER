# ADR 0002 - Canonical serialisation and hashing
UTF-8, sorted keys, no whitespace, NFC strings, Decimal as normalised plain string, floats and naive datetimes rejected,
datetimes as UTC microsecond `Z`. Hash = SHA-256(domain || 0x00 || canonical bytes). Evidence event hash covers
tenant (chain id), sequence, previous hash and every business field. A hash chain alone cannot detect privileged
truncation; detection requires an independently stored signed checkpoint (A6 E02).
