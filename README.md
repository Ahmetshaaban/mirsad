# 🔭 مرصد · Mirsad

**مرصد** أداة محلية تراقب صفحات الويب وتخبرك *بشو تغيّر فيها بالضبط* — بلا حساب، بلا اشتراك، وبلا مكتبات خارجية (Python قياسي فقط).

بتشتغل من سطر أوامر واحد، وبتعطيك **لوحة تحكم محلية** (عربي، على `127.0.0.1`) و**Webhooks JSON** جاهزة تشبكها بـ n8n أو أي شي.

> الفكرة مستوحاة من أدوات مثل Visualping وDistill.io وchangedetection.io — أُعيد بناء جوهرها من الصفر: تنزيل الصفحة → استخلاص المحتوى الرئيسي (Readability-style) → مقارنة لقطة بلقطة → عرض الفرق سطراً بسطر → تنبيه.

---

## ليش مرصد؟

- 🔍 **راقب أي شي بيتغيّر:** سعر منتج، صفحة وظائف، إعلان مورد، صفحة منافس، مخزون، حالة خدمة.
- 🔒 **محلي 100%:** اللقطات والفروقات على جهازك، ما في سيرفر خارجي ولا تتبّع.
- 🧩 **تكامل جاهز:** كل تغيير → `POST` JSON فيه الفرق — وصّلته بـ n8n؟ صار عندك تنبيه تلغرام/إيميل خلال دقيقة.
- 🪶 **صفر تثبيت:** ما في `pip install`. Python 3.8+ وخلص.
- 🤝 **مؤدب:** يحترم `robots.txt` افتراضياً، وتقدر تتجاوزه لكل هدف `--ignore-robots`.

## التشغيل السريع

```bash
# 1) فحص ذاتي (اختياري)
python mirsad.py selftest          # 12/12 passed

# 2) أضف صفحة — أول فحص يخزّن "الأساس" تلقائياً
python mirsad.py add https://example.com

# 3) أضف صفحة بسعر محدد فقط (CSS selector) مع webhook لـ n8n
python mirsad.py add "https://shop.example.com/item/123" --name item-123 \
       --selector "#price" --webhook http://127.0.0.1:5678/webhook/mirsad

# 4) افحص الكل
python mirsad.py check --all        # exit 0 = لا تغيير | 2 = في تغيير | 1 = خطأ

# 5) اللوحة المحلية
python mirsad.py serve --open       # http://127.0.0.1:8787
```

على ويندوز: `mirsad.cmd` نفس الأوامر بدون كتابة `python`.

## أوامر الأداة

| الأمر | بيعمل شو |
|---|---|
| `add URL [--name N] [--selector SEL] [--webhook URL] [--ignore-robots] [--no-check]` | يضيف هدف + فحص أساس فوري |
| `list [--json]` | جدول الأهداف وحالتها |
| `check [اسم…] [--all] [--json]` | فحص (الافتراضي: الكل) |
| `show [اسم]` | آخر فرق محفوظ |
| `set NAME [--url…] [--selector…] [--webhook…] [--note…] [--(no-)ignore-robots]` | تعديل إعدادات هدف |
| `remove NAME` | حذف هدف (السجل بيضل بمجلد data) |
| `serve [--port 8787] [--open]` | لوحة التحكم المحلية |
| `selftest` | فحوصات ذاتية بلا إنترنت |

**Selector** يدعم البسائط: `tag`، `#id`، `.class`، وتركيبها مع سلالات: `#price .value` أو `article h1`. بلا selector → يستخلص المحتوى الرئيسي تلقائياً (بيفلش القوائم والفوتر والإعلانات).

## أمثلة عملية

```bash
# سعر منتج من صفحة متجر
python mirsad.py add "https://store.example/product/x" --selector ".product-price"

# صفحة وظائف — راقب قسم جديد
python mirsad.py add "https://example-jobs.com/engineering" --name jobs-eng

# صفحة مورد على Alibaba/موقع مصنع — تتبّع تغيّر العروض
python mirsad.py add "https://manufacturer.example/catalog" --name supplier-a

# موقعك أنت — تأكد ما حدا غيّر شي
python mirsad.py set my-site --webhook http://127.0.0.1:5678/webhook/mirsad
```

## تكامل n8n

كل تغيير → `POST` بهاد الشكل (مثال حقيقي من اختبار):

```json
{
  "tool": "mirsad",
  "event": "changed",
  "name": "demo-store",
  "url": "http://127.0.0.1:8801/mutable/index.html",
  "checked_at": "2026-10-15T13:35:59+00:00",
  "added": 3,
  "removed": 3,
  "diff_excerpt": "--- before\n+++ after\n@@ ...\n-Price: 100 USD\n+Price: 90 USD\n...",
  "dashboard_url": "http://127.0.0.1:8787/diff/demo-store"
}
```

وصفة n8n: **Webhook node** (POST) → **Telegram/Slack/Email**. ويفضّل **Schedule Trigger** كل ساعة يشغّل `mirsad check --all` (أو استخدم جدولة ويندوز).

## الجدولة على ويندوز (بدون n8n)

```bat
schtasks /Create /TN "Mirsad Check" /SC HOUR /MO 1 /F ^
  /TR "\"C:\Users\SADEE\AppData\Local\Programs\Python\Python311\python.exe\" \"C:\Users\SADEE\Documents\mirsad\mirsad.py\" check --all"
```

> عدّل مسار Python حسب جهازك (`where python`).

## كيف بيشتغل داخلياً

1. **Fetch** — `urllib` مع UA واضح، مهلة وتكرارات، احترام `robots.txt`، فك ترميز ذكي (utf-8/windows-1256 للصفحات العربية القديمة).
2. **Extract** — محلل HTML متسامح + خوارزمية بمستوى Readability: تنقيط الكتل بحجم النص ناقص كثافة الروابط، أو selector مباشر.
3. **Normalize** — تنظيف السطور وتوحيد الفراغات (باش يطلع الفرق نظيف بلا ضجة).
4. **Diff** — `difflib` موحّد: عدد الأسطر المضافة/المحذوفة + ملف فرق محفوظ.
5. **Notify** — Webhook JSON + اللوحة المحلية (لوحة أهداف + عارض فرق ملوّن + عارض النص الكامل).

بنية البيانات:

```
data/
  config.json              # الأهداف والإعدادات
  snapshots/<name>/        # لقطات + latest.txt
  diffs/<name>/            # فروقات + latest.diff.txt
```

## الاستخدام المسؤول

- خليك مؤدب: التقط ما بالقدر المعقول (`--every` واعمل جدولة بالساعات مش بالثواني).
- الأداة تحترم `robots.txt` افتراضياً — لا تتجاوزها إلا على مواقعك أو لما يكون عندك إذن/سبب مشروع.
- بعض الصفحات اللي تبنى بالجافاسكربت ما تعطي نص بدون متصفح — استخدم selector على عنصر موجود بالـ HTML الأولي، أو راقب نسخة/API بديلة.

---

## English (brief)

**Mirsad** is a self-hosted, dependency-free page-change watcher (Python stdlib only). It freezes a readability-style text snapshot of any page, diffs it on each check, stores history under `data/`, shows a local Arabic-first dashboard (`serve`) and POSTs a JSON webhook on every change — ready for n8n. Supports per-target CSS-lite selectors (`#id`, `.class`, `tag`, descendant chains), honors robots.txt by default, works on Windows/Linux/macOS.

```bash
python mirsad.py add https://example.com --selector "#price" --webhook http://127.0.0.1:5678/webhook/mirsad
python mirsad.py check --all
python mirsad.py serve --open
```

## License

MIT — see [LICENSE](LICENSE).
