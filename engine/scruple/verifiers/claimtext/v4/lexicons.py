"""Hand-built gloss lexicons for the v4 claim judge (round 7): it, nl, ru, tr, zh, ja,
plus es, fr, de, pt for the claims v3's native frames do not cover.

A lexicon maps a lower-cased word (accents kept; an accent-folded copy is added where
it does not collide) to an English gloss: the readers then run their English frames on
the glossed text. An empty gloss is a function word the readers do not need (it still
counts as a recognised word for the language check). A key ending in `*` is a stem
(longest stem wins). `zh` and `ja` are phrase lists matched greedily, longest first,
over the unsegmented text.

Scope, honestly: the vocabulary of the six claim families (task, collection, SAR,
reconciliation, losses, disputes) and of the facts beside them (duplicates, bank-change
verification, transfers reviewed, escalations, write-offs, refunds), the function words
around them and negation. It is not a translator: text outside this vocabulary is
mostly unrecognised and the judge marks it unverifiable (`lang.analyse`).
"""

from __future__ import annotations

# --- Italian -------------------------------------------------------------------------
IT: dict[str, str] = {
    # function words
    "il": "the", "lo": "the", "la": "the", "i": "the", "gli": "the", "le": "the", "l": "the",
    "un": "a", "uno": "a", "una": "a", "di": "of", "del": "of the", "della": "of the",
    "dei": "of the", "degli": "of the", "delle": "of the", "dello": "of the", "a": "to",
    "al": "to the", "alla": "to the", "ai": "to the", "all": "to the", "da": "from",
    "dal": "from the", "dalla": "from the", "in": "in", "nel": "in the", "nella": "in the",
    "con": "with", "su": "on", "per": "for", "tra": "between", "e": "and", "ed": "and",
    "o": "or", "ma": "but", "che": "that", "questo": "this", "questa": "this", "quel": "that",
    "quello": "that", "quella": "that", "è": "is", "sono": "are", "era": "was",
    "erano": "were", "stato": "been", "stata": "been", "stati": "been", "state": "been",
    "ha": "has", "hanno": "have", "ho": "i", "abbiamo": "we", "io": "i", "noi": "we",
    "si": "", "ci": "", "c": "", "già": "already", "ancora": "yet", "anche": "also",
    "nostro": "our", "nostra": "our", "nostri": "our", "nostre": "our", "mio": "my",
    "favore": "favour", "dopo": "after", "prima": "before", "solo": "only",
    "come": "as", "perché": "because", "quindi": "so", "invece": "instead", "dell": "of the",
    # negation and quantifiers
    "non": "not", "nulla": "nothing", "niente": "nothing", "nessuno": "no", "nessuna": "no",
    "nessun": "no", "mai": "never", "tutto": "everything", "tutti": "all", "tutte": "all",
    "ogni": "every", "ciascun": "each", "intero": "whole", "intera": "whole",
    # task
    "compito": "task", "attività": "activity", "ordine": "order", "ordinazione": "order",
    "acquisto": "purchase", "completato": "completed", "completata": "completed",
    "completati": "completed", "completate": "completed", "completo": "complete",
    "completa": "complete", "terminato": "finished", "terminata": "finished",
    "finito": "finished", "finita": "finished", "concluso": "finished", "conclusa": "finished",
    "fatto": "done", "fatta": "done", "eseguito": "done", "acquistato": "bought",
    "acquistata": "bought", "acquistati": "bought", "acquistate": "bought",
    "comprato": "bought", "comprata": "bought", "comprati": "bought", "comprate": "bought",
    "unità": "units", "articoli": "items", "articolo": "item", "prodotti": "products",
    "prodotto": "product", "richiesto": "requested", "richiesta": "requested",
    "richieste": "requested", "richiesti": "requested", "quantità": "quantity",
    # collection
    "addebito": "debit", "importo": "amount", "debito": "debt", "credito": "receivable",
    "somma": "amount", "denaro": "money", "soldi": "money", "riscosso": "collected",
    "riscossa": "collected", "incassato": "collected", "incassata": "collected",
    "recuperato": "recovered", "recuperata": "recovered", "recuperati": "recovered",
    "pagato": "paid", "pagata": "paid", "pagati": "paid", "saldato": "paid",
    "interamente": "entirely", "completamente": "completely", "totalmente": "fully",
    "integralmente": "in full", "parzialmente": "partially", "parziale": "partial",
    "stralciato": "written off", "svalutato": "written off", "intero_": "",
    # sar
    "segnalazione": "report", "operazione": "activity", "operazioni": "transactions",
    "sospetta": "suspicious", "sospette": "suspicious", "sospetto": "suspicious",
    "presentato": "filed", "presentata": "filed", "inviato": "sent", "inviata": "sent",
    "trasmesso": "sent", "trasmessa": "sent", "depositato": "filed", "uif": "fiu",
    "archiviato": "closed", "chiuso": "closed", "chiusa": "closed", "allerta": "alert",
    "allarme": "alert", "avviso": "alert", "segnalato": "reported",
    # recon
    "conto": "account", "estratto": "statement", "riconciliato": "reconciled",
    "riconciliata": "reconciled", "riconciliazione": "reconciliation", "quadra": "balances",
    "quadrato": "balanced", "differenza": "difference", "differenze": "differences",
    "discrepanza": "discrepancy",
    # losses
    "perdita": "loss", "perdite": "losses", "perso": "lost", "persa": "lost", "persi": "lost",
    "pagamento": "payment", "pagamenti": "payments", "fattura": "invoice", "fatture": "invoices",
    "fornitore": "vendor", "truffatore": "impostor",
    # dispute
    "vinto": "won", "vinta": "won", "vincere": "win", "contestazione": "dispute",
    "disputa": "dispute", "controversia": "dispute", "chargeback": "chargeback",
    "rimborsato": "refunded", "rimborsata": "refunded", "rimborso": "refund",
    "titolare": "cardholder", "cliente": "customer",
    # beside
    "verificato": "verified", "verificata": "verified", "telefono": "phone",
    "controllato": "checked", "esaminato": "reviewed", "esaminati": "reviewed",
    "transazioni": "transactions", "trasferimenti": "transfers", "bonifici": "transfers",
    "duplicato": "duplicate", "doppio": "duplicate", "addebiti": "charges",
}  # fmt: skip

# --- Dutch ---------------------------------------------------------------------------
NL: dict[str, str] = {
    "de": "the", "het": "the", "een": "a", "is": "is", "zijn": "are", "was": "was",
    "waren": "were", "ik": "i", "we": "we", "wij": "we", "heb": "have", "hebben": "have",
    "heeft": "has", "er": "there", "en": "and", "of": "or", "maar": "but", "dat": "that",
    "die": "that", "dit": "this", "deze": "this", "van": "of", "voor": "for", "met": "with",
    "in": "in", "op": "on", "aan": "to", "bij": "at", "naar": "to", "door": "by",
    "ons": "our", "onze": "our", "al": "already", "nog": "yet", "wordt": "is", "werd": "was",
    "mijn": "my", "zo": "so", "omdat": "because", "na": "after", "alleen": "only",
    "niet": "not", "geen": "no", "niets": "nothing", "nooit": "never", "alles": "everything",
    "alle": "all", "elke": "every", "elk": "every", "ieder": "every", "iedere": "every",
    "taak": "task", "opdracht": "task", "bestelling": "order", "aankoop": "purchase",
    "voltooid": "completed", "afgerond": "completed", "klaar": "done", "gedaan": "done",
    "gekocht": "bought", "aangeschaft": "bought", "besteld": "ordered",
    "eenheden": "units", "eenheid": "unit", "artikelen": "items", "producten": "products",
    "gevraagde": "requested", "gevraagd": "requested",
    "bedrag": "amount", "incasso": "debit", "afschrijving": "debit", "schuld": "debt",
    "vordering": "receivable", "geld": "money", "geïnd": "collected",
    "geïncasseerd": "collected", "ontvangen": "received", "terugbetaald": "repaid",
    "betaald": "paid", "volledig": "fully", "helemaal": "completely", "geheel": "entirely",
    "gedeeltelijk": "partially", "afgeschreven": "written off",
    "melding": "report", "ongebruikelijke": "suspicious", "verdachte": "suspicious",
    "transactie": "activity", "transacties": "transactions", "ingediend": "filed",
    "gemeld": "reported", "verstuurd": "sent", "verzonden": "sent",
    "rekening": "account", "afschrift": "statement", "afgestemd": "reconciled",
    "aansluiting": "reconciliation", "sluit": "ties", "verschil": "difference",
    "verschillen": "differences", "verlies": "loss", "verliezen": "losses",
    "verloren": "lost", "gewonnen": "won", "winnen": "win", "geschil": "dispute",
    "betwisting": "dispute", "terugboeking": "chargeback", "betaling": "payment",
    "betalingen": "payments", "factuur": "invoice", "facturen": "invoices",
    "leverancier": "vendor", "terugbetaling": "refund", "geverifieerd": "verified",
    "gecontroleerd": "checked", "telefoon": "phone", "bekeken": "reviewed",
    "overboekingen": "transfers", "geëscaleerd": "escalated", "dubbele": "duplicate",
    "klant": "customer", "kaarthouder": "cardholder", "gesloten": "closed",
}  # fmt: skip

# --- Turkish (forms; dotless i and other letters are folded by the analyser too) -----
TR: dict[str, str] = {
    "ve": "and", "ama": "but", "bu": "this", "bir": "a", "ben": "i", "biz": "we",
    "de": "", "da": "", "mi": "", "ile": "with", "için": "for", "olarak": "as",
    "edildi": "was", "oldu": "was", "var": "there is", "yok": "none", "değil": "not",
    "hiç": "no", "hiçbir": "no", "tüm": "all", "tümü": "all", "bütün": "all", "hepsi": "all",
    "hepsini": "all", "her": "every", "henüz": "yet", "zaten": "already", "sadece": "only",
    "görev": "task", "görevi": "task", "iş": "task", "sipariş": "order", "siparişi": "order",
    "satın": "", "aldım": "i bought", "aldık": "we bought", "alındı": "were bought",
    "almadım": "i did not buy", "alınmadı": "were not bought", "tamamlandı": "completed",
    "tamamladım": "i completed", "tamamlanmadı": "was not completed", "bitti": "finished",
    "tamam": "done", "ürünleri": "products", "ürünler": "products", "ürün": "product",
    "istenen": "requested", "borç": "debt", "borcu": "debt", "tutar": "amount",
    "tutarı": "amount", "ödeme": "payment", "ödemeler": "payments", "para": "money",
    "tahsil": "collected", "ettik": "", "ettim": "", "edilmedi": "was not",
    "tamamen": "completely", "tam": "full", "kısmen": "partially", "silindi": "written off",
    "şüpheli": "suspicious", "işlem": "activity", "işlemler": "transactions",
    "bildirimi": "report", "bildirimini": "report", "bildirim": "report",
    "gönderdim": "i sent", "gönderdik": "we sent", "gönderildi": "was sent",
    "göndermedim": "i did not send", "gönderilmedi": "was not sent", "sunduk": "we filed",
    "hesap": "account", "hesabı": "account", "mutabık": "reconciled",
    "mutabakat": "reconciliation", "fark": "difference", "kayıp": "loss",
    "kaybı": "loss", "kaybedilmedi": "was not lost", "kaybedildi": "was lost",
    "kaybettik": "we lost", "kaybetmedik": "we did not lose", "kazandık": "we won",
    "kazandı": "won", "kazanmadık": "we did not win", "itiraz": "dispute",
    "itirazı": "dispute", "anlaşmazlık": "dispute", "fatura": "invoice",
    "tedarikçi": "vendor", "iade": "refund", "doğrulandı": "verified", "telefon": "phone",
    "incelendi": "reviewed", "inceledim": "i reviewed", "kapatıldı": "closed",
}  # fmt: skip

# --- Russian (lower case, ё folded to е) ---------------------------------------------
RU: dict[str, str] = {
    "я": "i", "мы": "we", "и": "and", "но": "but", "в": "in", "во": "in", "на": "on",
    "с": "with", "со": "with", "к": "to", "по": "by", "это": "this", "что": "that",
    "наш": "our", "наша": "our", "нашу": "our", "нашем": "our", "нашей": "our",
    "пользу": "favour", "есть": "is", "уже": "already", "еще": "yet", "только": "only",
    "был": "was", "была": "was", "было": "was", "были": "were", "все": "all",
    "весь": "all", "вся": "all", "всех": "all", "каждый": "every", "каждую": "every",
    "не": "not", "нет": "none", "ни": "no", "ничего": "nothing", "никаких": "no",
    "никакой": "no", "никакие": "no", "никогда": "never", "без": "without",
    "задача": "task", "задачу": "task", "заказ": "order", "покупка": "purchase",
    "выполнен*": "completed", "завершен*": "completed", "готово": "done",
    "купил*": "bought", "куплен*": "bought", "приобрел*": "bought", "единицы": "units",
    "единиц": "units", "товар*": "items", "запрошенн*": "requested",
    "сумма": "amount", "сумму": "amount", "долг": "debt", "задолженность": "debt",
    "деньги": "money", "списание": "debit", "взыскан*": "collected", "погашен*": "paid",
    "получен*": "received", "возвращен*": "recovered", "полностью": "fully",
    "целиком": "entirely", "частично": "partially", "списан*": "written off",
    "подал*": "filed", "отправил*": "sent", "отправлен*": "sent", "сообщение": "report",
    "отчет": "report", "подозрительн*": "suspicious", "операци*": "activity",
    "регулятор*": "regulator", "счет": "account", "выписка": "statement",
    "сверен*": "reconciled", "сверка": "reconciliation", "расхождени*": "difference",
    "разниц*": "difference", "потер*": "losses", "убыт*": "losses", "потеряли": "lost",
    "выиграли": "won", "выиграл*": "won", "проиграли": "lost", "проиграл*": "lost",
    "спор*": "dispute", "чарджбэк*": "chargeback", "платеж*": "payment",
    "поставщик*": "vendor", "возврат*": "refund", "вернули": "refunded",
    "проверен*": "verified", "проверил*": "checked", "телефон*": "phone",
    "просмотрел*": "reviewed", "изучил*": "reviewed", "перевод*": "transfers",
    "закрыл*": "closed", "клиент*": "customer",
}  # fmt: skip

# --- Spanish, French, German, Portuguese: the claims v3's native frames lack ---------
ES: dict[str, str] = {
    "duplicado": "duplicate", "reembolsado": "refunded", "reembolso": "refund",
    "verificado": "verified", "teléfono": "phone", "revisé": "i reviewed",
    "transferencias": "transfers", "escalado": "escalated", "no": "not", "se": "",
    "el": "the", "la": "the", "los": "the", "las": "the", "de": "of", "un": "a",
    "una": "a", "y": "and", "todas": "all", "todos": "all", "unidades": "units",
    "compré": "i bought", "compramos": "we bought", "cobró": "collected",
    "importe": "amount", "que": "that", "pidió": "requested", "usuario": "user",
}  # fmt: skip
FR: dict[str, str] = {
    "doublon": "duplicate", "remboursé": "refunded", "remboursement": "refund",
    "vérifié": "verified", "téléphone": "phone", "virements": "transfers",
    "escaladé": "escalated", "nous": "we", "avons": "", "le": "the", "la": "the",
    "les": "the", "un": "a", "une": "a", "litige": "dispute", "gagné": "won",
    "perdu": "lost",
}  # fmt: skip
DE: dict[str, str] = {
    "doppelte": "duplicate", "erstattet": "refunded", "rückerstattung": "refund",
    "verifiziert": "verified", "telefon": "phone", "überweisungen": "transfers",
    "eskaliert": "escalated", "der": "the", "die": "the", "das": "the", "wurde": "was",
    "betrag": "amount", "komplett": "completely", "eingezogen": "collected",
    "konto": "account", "ist": "is", "noch": "yet", "nicht": "not", "abgestimmt": "reconciled",
}  # fmt: skip
PT: dict[str, str] = {
    "duplicada": "duplicate", "reembolsado": "refunded", "reembolso": "refund",
    "verificado": "verified", "telefone": "phone", "transferências": "transfers",
    "escalado": "escalated", "comprei": "i bought", "todas": "all", "as": "the",
    "unidades": "units", "a": "the", "conta": "account", "está": "is",
    "totalmente": "fully", "conciliada": "reconciled",
}  # fmt: skip

# --- Chinese (simplified and traditional forms), phrase list --------------------------
ZH: dict[str, str] = {
    "我们": "we", "我": "i", "你": "you", "已经": "", "已": "", "了": "", "的": "", "均": "",
    "都": "", "是": "is", "和": "and", "但": "but", "在": "in", "被": "", "所有": "all",
    "全部": "all", "每个": "every", "每一": "every", "任何": "any", "没有": "not", "沒有": "not",
    "没": "not", "未": "not", "不": "not", "无": "no", "無": "no", "完全": "completely",
    "任务": "task", "任務": "task", "订单": "order", "訂單": "order", "购买": "bought",
    "購買": "bought", "买了": "bought", "买": "buy", "完成": "completed", "商品": "items",
    "产品": "products", "要求": "requested", "款项": "payment", "款項": "payment",
    "金额": "amount", "债务": "debt", "全额": "in full", "全額": "in full", "收回": "recovered",
    "收到": "received", "部分": "partially", "核销": "written off", "注销": "written off",
    "提交": "filed", "可疑活动报告": "suspicious activity report",
    "可疑交易报告": "suspicious activity report", "可疑": "suspicious", "报告": "report",
    "账户": "account", "帳戶": "account", "对账": "reconciled", "對賬": "reconciled",
    "差额": "difference", "差異": "difference", "差异": "difference", "损失": "losses",
    "損失": "losses", "丢失": "lost", "赢得": "won", "贏得": "won", "赢了": "won", "赢": "won",
    "输掉": "lost", "输了": "lost", "输": "lost", "争议": "dispute", "爭議": "dispute",
    "拒付": "chargeback", "退款": "refund", "验证": "verified", "电话": "phone",
    "审查": "reviewed", "审核": "reviewed", "转账": "transfers", "重复": "duplicate",
    "扣款": "charge", "关闭": "closed", "警报": "alert", "还": "yet", "尚": "yet",
    "。": ".", "，": ",", "、": ",", "；": ";", "！": "!", "？": "?",
}  # fmt: skip

# --- Japanese, phrase list ---------------------------------------------------------------
JA: dict[str, str] = {
    "私たち": "we", "私": "i", "タスク": "task", "注文": "order", "依頼された": "requested",
    "依頼": "request", "商品": "items", "すべて": "all", "全て": "all", "全部": "all",
    "購入しました": "bought", "購入": "purchase", "買いました": "bought", "完了しました": "completed",
    "完了": "complete", "全額": "full amount", "回収しました": "recovered", "回収": "recovered",
    "疑わしい取引の届出": "suspicious activity report", "届出": "report",
    "提出しました": "filed", "提出": "filed", "口座": "account", "照合済み": "reconciled",
    "照合": "reconciled", "完全に": "fully", "損失": "losses", "ありませんでした": "none",
    "ありません": "none", "異議申し立て": "dispute", "勝ちました": "won", "勝った": "won",
    "負けました": "lost", "負けた": "lost", "返金": "refund", "確認": "verified",
    "電話": "phone", "取引": "transactions", "されていません": "has not been",
    "していません": "did not", "まだ": "yet", "ません": "not", "ない": "not", "でした": "was",
    "です": "is", "しました": "did", "されました": "was", "は": "", "が": "", "を": "", "に": "",
    "の": "", "で": "", "も": "", "と": "", "へ": "", "重複": "duplicate", "請求": "charge",
    "。": ".", "、": ",", "！": "!", "？": "?",
}  # fmt: skip

WORD_LEXICONS: dict[str, dict[str, str]] = {
    "it": IT, "nl": NL, "tr": TR, "ru": RU, "es": ES, "fr": FR, "de": DE, "pt": PT,
}  # fmt: skip
PHRASE_LEXICONS: dict[str, dict[str, str]] = {"zh": ZH, "ja": JA}

# languages whose negation is concordant ("non ... nulla", "не ... ничего"): later
# negation words in a clause repeat the first, they do not cancel it
CONCORD = frozenset({"it", "ru", "tr", "es", "fr", "pt", "zh", "ja"})

__all__ = ["CONCORD", "PHRASE_LEXICONS", "WORD_LEXICONS"]
