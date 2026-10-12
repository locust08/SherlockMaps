# SherlockMaps

## Email-crawl deadlines

The email crawler follows websites belonging to an existing Maps job. It does
not start Maps collection or send email.

The API reads these environment variables at startup (positive finite seconds):

| Variable | Default | Behaviour |
| --- | --- | --- |
| `EMAIL_WEBSITE_TIMEOUT_SECONDS` | `300` | Skip a website that exceeds five minutes; keep addresses already found and continue. |
| `EMAIL_JOB_TIMEOUT_SECONDS` | `600` | Fail an email job after ten minutes and release the queue for the next job. |
| `EMAIL_CLEANUP_TIMEOUT_SECONDS` | `10` | Bound page/browser cleanup and cancellation acknowledgement. |

Job deadlines include browser initialization and crawling. Cancellation may add
up to the cleanup allowance before the queue is released. Website cancellation
that does not settle within that allowance fails the job rather than reusing
potentially unsafe extractor state. Browser context, browser and driver cleanup
are attempted independently so one stuck resource cannot block the others.

Restart the Python API to apply code or environment changes. Timed-out jobs have
a failed status and a timeout error; they are not automatically retried. A job
deadline discards that unfinished job's result batch, while already completed
jobs remain saved. These async safeguards require a responsive Python event
loop; they are not an operating-system watchdog for process freezes.
