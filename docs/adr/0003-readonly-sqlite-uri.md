# Safe Read-Only SQLite Access for Credit Metering

WorkBuddy continuously updates `~/.workbuddy/workbuddy.db` during active chat generation under SQLite WAL mode. To eliminate file-locking contention without incurring disk I/O snapshot copies, `CreditMeter` opens the database strictly using SQLite read-only URI mode (`file:{path}?mode=ro`, `uri=True`) with a 5.0-second busy timeout. This guarantees non-blocking concurrent reads alongside active client writing while observing the latest uncommitted WAL frame data without risking database corruption.
