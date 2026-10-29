# Promoted regressions

`reduced-duplicate-debit.yaml` is the verified smaller reproduction from M3 reduction group
`23dd08e5-d2eb-4417-97ca-b389894a2a55` (20 operations reduced to 1, 5/5 validation). Source run `d37f91fe-d2c6-4b91-a228-21cf3eec52a3`, target `orders.at_most_one_debit` / `duplicate_debit` / `purchase-a`.

Its business inputs, fixture (`reduction_market`), fault rule, and retry are unchanged; only the
scenario `id` was renamed. It fails against `unsafe` and joins the ordinary correct-variant suite
in `../corpus.json`, so the fixed applications must keep passing it.
