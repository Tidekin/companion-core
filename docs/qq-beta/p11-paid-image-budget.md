# P11 A2: single-image paid admission

Paid image input is disabled unless `paid_model.image_input` has a separate
`approval_sha256`, positive `estimated_tokens_per_image`, `max_bytes_per_image`
(at most 4,000,000), `max_pixels_per_image` (at most 16,000,000), and
`max_images_per_request=1`. The estimate is an operator-configured planning value,
not proof of a provider's tokenizer, price, or upper charge. The text estimator
applies only to the accompanying text; the image estimate is added before the
existing safety percentage. Base64 length is never counted as text tokens.

The first contract supports one current owner image and no history or memory.
Content and live-window authorization must separately prove its source, epoch,
approval, content lease and frozen payload. This budget seam does not grant those
permissions. The approved window retains its total five-attempt cap.

`content.use_chat_model_for_vision=true` selects the same approved provider, model,
endpoint and key as chat. A separate media provider is unsupported. The exact
messages are one `user` message with an ordered text part and one inline
`data:image/png|jpeg|webp;base64,...` image part. Remote URLs, extra parts,
multiple images, animated images, mismatched MIME and noncanonical encoding are
rejected before admission. Dimensions are also limited to 4096 on either side.

Content passes the actual chat `job_id` to the existing `budget.reserve`,
`budget.complete`, and `budget.settle` functions. The logical request is that same
job, `per_request_calls=2`, with at most one vision attempt followed by chat.
The vision output cap is `min(max_output_tokens, 300)`. Both attempts use the
existing admissions, usage rows and BudgetJournal, along with request/window/day
call limits, token bounds and concurrency. Monetary caps and allocations are
removed from current configuration. No extra job, queue or accounting ledger exists.

The original ModelClient binds the actual multimodal HTTP payload to its single
use admission after request hooks. Budget rechecks configuration, policy, job,
stage, logical request, current leases and window sources before dispatch.
Actual returned token usage uses the existing settlement path, even when the
provider rejects the request or returns unusable text. Content settles before
accepting results or rejecting changed/deleted sources. Missing or invalid usage
remains UNKNOWN and retains its reservation; it does not create a free retry.

When `image_input` is absent, it and the image-only snapshot fields are omitted.
Existing text contract snapshots retain their original canonical bytes. Enabling
or changing image policy changes the paid contract and requires an explicitly
approved successor rather than rewriting an existing budget window.

Validation here uses synthetic images and Mock HTTP. It does not authorize a QQ
window, prove a provider supports images, or establish actual provider billing.
