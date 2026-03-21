# Manual Validation Batch 1 (14 cases)

Decision codes:
- `KEEP_NO_REPLY` = оставляем кейс как валидный кейс без исходящего ответа.
- `REPAIR_PAIR` = в thread есть история, но пара inbound/outbound в dataset сдвинута; нужно перепривязать.
- `DROP_SYNTH` = synthetic/test кейс, исключаем из Gmail-ground-truth prompt eval.
- `NEEDS_REAL_THREAD` = нужен реальный gmail_thread_id, без него кейс невалиден.

## Case 1154 (new_order)
- Status: `resolved_thread`
- Thread: `19cfc936577f39c4`
- Matched inbound ts (UTC): `2026-03-17T17:03:03+00:00`
- Matched inbound snippet: `Paid, thanks.  On Tue, Mar 17, 2026 at 11:34 AM James Harris <getorderstick@gmail.com> wrote:  > Thank you so much for placing an order > *Your total is $110.00 FREE shipping (1 x Terea Amber ME`
- Dataset inbound preview: `From: Shipmecarton <order@shipmecarton.com> Reply-To: email2kevin@gmail.com Subject: Shipmecarton - Order 23662 Body: # 			Image 			Name 			Price 			Qnt 			Amount 		 					 				1 		`
- Dataset outbound preview: `Thank you so much for placing an order Your total is $110.00 FREE shipping  !!! Zelle ( In memo or comments don't put anything please ! ) use email below  nicezellepay@gmail.com  I`
- Recommended decision: `REPAIR_PAIR`
- Note: В thread после matched inbound нет исходящего; вероятно в dataset сохранена более ранняя пара или неверный шаг диалога.

## Case 1131 (new_order)
- Status: `resolved_thread`
- Thread: `19cfbc48f160c820`
- Matched inbound ts (UTC): `2026-03-18T02:28:06+00:00`
- Matched inbound snippet: `Do you have tracking?  On Tue, Mar 17, 2026, 17:05 Philip Battaglia <pcbattaglia@gmail.com> wrote:  > Paid, thanks. > > On Tue, Mar 17, 2026, 10:11 James Harris <getorderstick@gmail.com> wrote:`
- Dataset inbound preview: `From: Shipmecarton <order@shipmecarton.com> Reply-To: pcbattaglia@gmail.com Subject: Shipmecarton - Order 23659 Body: # 			Image 			Name 			Price 			Qnt 			Amount 		 					 				1 		`
- Dataset outbound preview: `Thank you so much for placing an order Your total is $230.00 FREE shipping  !!! Zelle ( In memo or comments don't put anything please ! ) use email below    If paid today, We will `
- Recommended decision: `REPAIR_PAIR`
- Note: В thread после matched inbound нет исходящего; вероятно в dataset сохранена более ранняя пара или неверный шаг диалога.

## Case 1145 (stock_question)
- Status: `resolved_thread`
- Thread: `19cfa0982696d191`
- Matched inbound ts (UTC): `2026-03-18T00:37:00+00:00`
- Matched inbound snippet: `Hello let me know if I need to place an order for the yellow via your website or if we can confirm on here . Thank you    Sent from Yahoo Mail for iPhone   On Tuesday, March 17, 2026, 11:18 AM, `
- Dataset inbound preview: `From: mariah ezz <mar92a@yahoo.com> Subject: Re: Shipmecarton - Order 23656 Body: Hello ! Do you have in stock any terra russet or bronze ?   Sent from Yahoo Mail for iPhone  `
- Dataset outbound preview: `Hi Maria Hannah, Russet ME and Bronze ME are not available right now. We can swap with Sun Pearl ME ($110/box), BLUE ME ($110/box), or Yellow ME ($110/box). Do any of those work fo`
- Recommended decision: `REPAIR_PAIR`
- Note: В thread после matched inbound нет исходящего; вероятно в dataset сохранена более ранняя пара или неверный шаг диалога.

## Case 574 (stock_question)
- Status: `resolved_thread`
- Thread: `19cc435f8f4c6966`
- Matched inbound ts (UTC): `2026-03-12T10:01:48+00:00`
- Matched inbound snippet: `Don't take t smoothI buy warm terea japan ship from ca   Được gửi từ Yahoo Mail cho iPhone   Vào Thứ Sáu, tháng 3 6, 2026, 05:32, James Harris <getorderstick@gmail.com> đã viết:  Hi Tung, we h`
- Dataset inbound preview: `From: Tung <vietboyz84@yahoo.com> Subject: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc: Re: Về việc`
- Dataset outbound preview: `Hi Tung, yes, we have Terea Lemon made in Japan in stock from our California warehouse! It's $115 per box. Let us know how many boxes you'd like and we'll get it ready for you. Tha`
- Recommended decision: `REPAIR_PAIR`
- Note: В thread после matched inbound нет исходящего; вероятно в dataset сохранена более ранняя пара или неверный шаг диалога.

## Case 808 (payment_question)
- Status: `resolved_thread`
- Thread: `19cd3cfb8a9687b7`
- Matched inbound ts (UTC): `2026-03-09T18:15:18+00:00`
- Matched inbound snippet: `Yes, please proceed with the order.  On Mon, Mar 9, 2026, 14:14 Shipmecarton <order@shipmecarton.com> wrote:  > Please make sure all info below is correct > Payment Instructions > > > > *Hi !`
- Dataset inbound preview: `From: Taisiia Rozhok <taisiia.rozhok@gmail.com> Subject: Re: Shipmecarton - Order 23618 Body: Yes, please proceed with the order.  On Mon, Mar 9, 2026, 14:14 Shipmecarton <order@`
- Dataset outbound preview: `Hi Taisiia, perfect thanks for confirming! For order 23618 the total is $550.00, and you can pay via Zelle (preferred) or Cash App when you receive your order. In memo or comments `
- Recommended decision: `REPAIR_PAIR`
- Note: В thread после matched inbound нет исходящего; вероятно в dataset сохранена более ранняя пара или неверный шаг диалога.

## Case 871 (other)
- Status: `resolved_thread`
- Thread: `19cd8bf84468eda2`
- Matched inbound ts (UTC): `2026-03-10T17:15:40+00:00`
- Matched inbound snippet: `Yes, everything is correct.  On Tue, Mar 10, 2026, 11:10 AM Shipmecarton <order@shipmecarton.com> wrote:  > Please make sure all info below is correct > Payment Instructions > > > > *Hi ! Our`
- Dataset inbound preview: `From: Gary Yamasaki <garykoyamasaki@gmail.com> Subject: Re: Shipmecarton - Order 23624 Body: Yes, everything is correct.  On Tue, Mar 10, 2026, 11:10 AM Shipmecarton <order@shipm`
- Dataset outbound preview: ``
- Recommended decision: `KEEP_NO_REPLY`
- Note: После inbound нет исходящего; для `other` это может быть корректная тишина (spam/confirm-only).

## Case 666 (other)
- Status: `resolved_thread`
- Thread: `19cc9d13bcc7d654`
- Matched inbound ts (UTC): `2026-03-07T19:40:45+00:00`
- Matched inbound snippet: ``
- Dataset inbound preview: `From: Diana Havryliuk <dana.sergeevna17@gmail.com> Subject: Order 23602 Body:`
- Dataset outbound preview: ``
- Recommended decision: `KEEP_NO_REPLY`
- Note: После inbound нет исходящего; для `other` это может быть корректная тишина (spam/confirm-only).

## Case 424 (other)
- Status: `resolved_thread`
- Thread: `19cbbd0df7933098`
- Matched inbound ts (UTC): `2026-03-05T02:25:53+00:00`
- Matched inbound snippet: `Need more clicks and conversions for shipmecarton.com? Watch this short video about our AI-powered traffic service: https://www.youtube.com/shorts/m26AoaWSFII`
- Dataset inbound preview: `From: Genesis Leavens <order@shipmecarton.com> Reply-To: leavens.genesis70@gmail.com Subject: Enquiry Genesis Leavens Body: Need more clicks and conversions for shipmecarton.com? W`
- Dataset outbound preview: ``
- Recommended decision: `KEEP_NO_REPLY`
- Note: После inbound нет исходящего; для `other` это может быть корректная тишина (spam/confirm-only).

## Case 286 (other)
- Status: `resolved_thread`
- Thread: `19cb43947cc13548`
- Matched inbound ts (UTC): `2026-03-03T15:02:37+00:00`
- Matched inbound snippet: `Yes, everything is correct.    On Tue, Mar 3, 2026, 9:02 AM Shipmecarton <order@shipmecarton.com> wrote:  > Please make sure all info below is correct > Payment Instructions > > > > *Hi ! O`
- Dataset inbound preview: `From: JASON MILLER <millerjason80@gmail.com> Subject: Re: Shipmecarton - Order 23581 Body: Yes, everything is correct.    On Tue, Mar 3, 2026, 9:02 AM Shipmecarton <order@shipm`
- Dataset outbound preview: ``
- Recommended decision: `KEEP_NO_REPLY`
- Note: После inbound нет исходящего; для `other` это может быть корректная тишина (spam/confirm-only).

## Case 281 (other)
- Status: `resolved_thread`
- Thread: `19cb27a63c7680a0`
- Matched inbound ts (UTC): `2026-03-03T06:54:49+00:00`
- Matched inbound snippet: `What if shipmecarton.com could harness TikTok for genuine leads? Our smart AI growth service zeros in on the perfect users—based on hashtags they’re into and accounts they follow—to supercharge your r`
- Dataset inbound preview: `From: Lakesha Carlton <order@shipmecarton.com> Reply-To: carlton.lakesha@gmail.com Subject: Enquiry Lakesha Carlton Body: What if shipmecarton.com could harness TikTok for genuine `
- Dataset outbound preview: ``
- Recommended decision: `KEEP_NO_REPLY`
- Note: После inbound нет исходящего; для `other` это может быть корректная тишина (spam/confirm-only).

## Case 329 (stock_question)
- Status: `thread_not_found`
- Thread: `test-drew-oos-thread`
- Dataset inbound preview: `From: dschmidt95@gmail.com Subject: Re: Your order  Thanks\! Do you have Tropical in stock though?`
- Dataset outbound preview: `Hi Drew Schmidt, yes, we have Tropical in stock! It's $115 per box. Let us know how many boxes you'd like and we'll get it ready for you. Thank you!`
- Recommended decision: `DROP_SYNTH`
- Note: `gmail_thread_id` невалидный (test/sim id), Gmail API возвращает invalidArgument.

## Case 346 (other)
- Status: `thread_not_found`
- Thread: `test-gheorghe-fresh-001`
- Dataset inbound preview: `From:  Subject: Shipmecarton - Order 23549 Body: # 			Image 			Name 			Price 			Qnt 			Amount 		 					 				1 				 				Tera MAUVE WAVE made in Europe  														 				$110.00 			`
- Dataset outbound preview: ``
- Recommended decision: `DROP_SYNTH`
- Note: `gmail_thread_id` невалидный (test/sim id), Gmail API возвращает invalidArgument.

## Case 343 (other)
- Status: `thread_not_found`
- Thread: `sim-order-23561-thread-test2`
- Dataset inbound preview: `From: noreply@shipmecarton.com Reply-To: jillanov@gmail.com Subject: Shipmecarton - Order 23561 Body: <!DOCTYPE html> <html> <body> <h2>New Order Received</h2> <p>You have received`
- Dataset outbound preview: ``
- Recommended decision: `DROP_SYNTH`
- Note: `gmail_thread_id` невалидный (test/sim id), Gmail API возвращает invalidArgument.

## Case 337 (other)
- Status: `thread_not_found`
- Thread: `sim-thread-23561`
- Dataset inbound preview: `From: noreply@shipmecarton.com Reply-To: jillanov@gmail.com Subject: Shipmecarton - Order 23561 Body: <!DOCTYPE html> <html> <body> <h2>New Order Received</h2> <p>You have received`
- Dataset outbound preview: ``
- Recommended decision: `DROP_SYNTH`
- Note: `gmail_thread_id` невалидный (test/sim id), Gmail API возвращает invalidArgument.
