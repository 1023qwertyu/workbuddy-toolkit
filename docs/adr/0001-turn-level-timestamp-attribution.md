# Turn-Level Timestamp Attribution for Credit Metering

WorkBuddy stores session-level `updated_at` alongside a JSON map of request deductions (`credit_json`). When computing credits consumed within a time window, we parse turn timestamps directly from request IDs (UUIDv7 prefixes) and conversation events rather than relying solely on the session's `updated_at`. This prevents multi-day conversational sessions from erroneously attributing historical credit consumption to the latest active day.
