# Historical case recovery

[README](../README.md) · [Operator runbook](runbook.md)

These procedures apply only to the named document versions and existing publication state.
For the normal workflow, see [Review and sharing](review-and-sharing.md).

## Locally correct the saved Књажевац БМ 61 copy

Already-sent Viber messages cannot be edited retroactively. This guarded command changes only the
locally saved copy displayed by `case/publication`; it does not call Drive, change the public folder or
files, alter the approved full summary/PDF, inspect OAuth material, or send Viber.

1. Restart/refresh the local case page and copy the exact **document URL** for the intended Књажевац
   БМ 61 version. Do not guess a station-wide selector. Preview against the explicit election DB:

       uv run rg2watch revise-viber --db data/election-677084.sqlite --url '<exact document URL from the local case page>' --reason 'Потребна је провера броја гласача ван бирачког места (18 од 58) и броја гласова за једну листу (53 од 58).'

2. Verify the printed municipality/station, original saved message, proposed four-line message, case
   link and RIK link. The preview opens SQLite read-only. Stop if any value is unexpected.
3. Only after that verification, repeat the same command with `--confirm`. The command rechecks all
   frozen identifiers and the exact legacy message under one SQLite write lock, then changes only
   `viber_text` and `updated_at`. A repeat is a no-op; any different existing copy requires
   investigation rather than overwrite.
4. Restart `serve` if needed and refresh the case page. Confirm the same public case URL remains, both
   files still open in a logged-out view, **RG2 Watch** remains restricted, and the corrected Serbian
   copy appears before manually copying/sending it. Do not create a second publication or change Drive
   permissions.

## One-time recovery of the pending election 677084 case

The previously attempted folder must be recovered manually; rg2watch never moves, adopts, deletes or
replaces it. Treat the report that it is empty and currently in My Drive as unverified until checking:

1. Run only `uv run rg2watch drive-root --config rg2watch.toml`, open its printed URL, and confirm
   **RG2 Watch** is directly under My Drive and restricted. If creation, identity, location or privacy
   verification fails, stop without moving or sharing anything. This retry does not move the existing
   pending case folder or publish/send a Viber message.
2. In the Google Drive UI, open the existing `RG2 предмет — Књажевац БМ 61` folder and inspect it for
   **any** files. An app-visible listing is not proof of emptiness. Stop and investigate if it is not
   empty.
3. Immediately after confirming it is empty, use the Drive UI to move that same folder into **RG2
   Watch**. Do not create a replacement. Confirm both the parent and case folder are still restricted.
4. Restart the local `serve`, open the pending case, verify that the frozen Serbian text is approved,
   explicitly confirm the warning, and click **Retry frozen publication**.
5. In Drive, confirm `My Drive / RG2 Watch / RG2 предмет — Књажевац БМ 61` is the same folder and now
   contains exactly the PDF and text file. The app checks the exact parent and original preallocated
   IDs; if the folder is still at My Drive root or elsewhere it stops with a manual-move instruction.
6. Before sending or pasting the Viber message, open the case link and both children in a logged-out
   private browser. The case folder alone must have “Anyone with the link — Viewer”; **RG2 Watch** must
   remain restricted. If Google policy blocks inherited child access, stop at the displayed error and
   do not send the link.
