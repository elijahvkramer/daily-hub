# iPhone install + test script (build 10-10s1)

Have the computer open at https://elijahvkramer.github.io/daily-hub/ (unlocked) for the sync steps. Tick each line; note anything that differs.

## A. Install (one time)
1. On the iPhone, open **Safari** → `elijahvkramer.github.io/daily-hub`.
   - Expect: a card at the bottom, "Get the Daily Hub app".
2. Tap **Share** (newer iOS: tap **•••** by the address bar, then **Share**) → **Add to Home Screen** → keep the name "Daily Hub" → **Add**.
   - Expect: a blue-diamond icon with no black corners.
3. Open it from the Home Screen.
   - Expect: launch screen ("DAILY HUB"), then full screen with no Safari bars; clock/battery readable; nothing under the notch or home bar.
4. Enter your passphrase.
   - Expect: Home opens; a bar offers Face ID → accept it and confirm with Face ID.
5. Swipe the app away, reopen.
   - Expect: Face ID prompt, then Home. No passphrase.

## B. Sync (phone ↔ computer, each within ~5 s)
6. Phone: add to-do "Phone test 1". → Computer shows it.
7. Computer: check it off. → Phone shows it done.
8. Phone and computer at the same time: phone renames one to-do, computer adds "Computer test 2". → Both changes on both screens.
9. Phone: drag to reorder two to-dos. → Same order on computer.
10. Computer: delete "Phone test 1"; phone: restore it from Recently deleted. → Back once on both (not twice).
11. Phone: add countdown "Phone chip" (any future date). → Shows on computer. Delete it on the computer → gone on phone.
12. Treatment tracker: phone taps **Took** on one med; computer taps **Took** on the same med within a few seconds. → Count goes up by 2 on both. Undo on phone → down by 1 on both.
13. Friends: log a catch-up on the phone. → Computer shows the new "last touch".
14. Tracker footer on both reads "Synced across devices · build 10-10s1".

## C. Offline
15. Phone: Airplane Mode on. Add to-do "Offline 1", tap Took on a med.
    - Expect: both appear immediately; footer says "Saved on this device · syncs when you're back online".
16. Swipe the app away, reopen (still offline).
    - Expect: app opens with your data, including "Offline 1".
17. Airplane Mode off, return to the app.
    - Expect: within ~10 s both appear on the computer; footer back to "Synced"; nothing duplicated.

## D. Updates
18. Tell me "push a test update". I'll deploy a visible change.
19. Background the app (go Home) for a few seconds, then reopen it.
    - Expect: the app reloads on its own and shows the change; you stay unlocked.
20. Repeat, but while typing in a to-do field when you return.
    - Expect: no reload; a "New version ready · tap to update" pill instead.

## Clean-up
Delete "Phone test 1", "Computer test 2", "Offline 1", and undo the test doses (Undo on the tracker).
