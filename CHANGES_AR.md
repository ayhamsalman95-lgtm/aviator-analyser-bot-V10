# تقرير التعديل — Aviator Telegram Bot

## ما تم فحصه قبل التعديل

تم فحص نسخة Grok الحالية ونسخة الالتقاط القديمة `game_network.jsonl`.
في الالتقاط القديم، WebSocket اللعبة الحقيقي هو:

`wss://1xlite-130003.top/games-frame/sockets/crash`

والأحداث الفعلية التي تصل تتضمن `OnStart` و`OnCrash`، بينما `OnCrash` الذي تمت مراجعته يحتوي على حقول الجولة/النتيجة مثل `l` و`f` و`ts`.

لم تظهر في الالتقاط القديم قيم فعلية باسم `serverSeed` أو `clientSeed` أو `seedHash` داخل البيانات التي تم حفظها. لذلك لم يتم اختراع Endpoint أو بذور غير موجودة.

## أسباب العطل التي تم إصلاحها

1. الجامع كان يفحص بعض Response URLs فقط إذا احتوت على كلمات محددة، ما قد يفوّت Endpoint حقيقي لبيانات العدل.
2. كان يجمع HTML من الصفحة لكنه لا يحلله فعليًا كمصدر مستقل للبذور.
3. مراقبة البذور كانت تملك حالة واحدة للجولة، وبالتالي قد تضيع Server Seed إذا وصلت بعد `OnCrash`.
4. حتى عند وصول البذرة المتأخرة، لم يكن هناك مسار مضمون لإرجاعها إلى الجولة المحفوظة وإعادة التحقق منها.
5. Telegram كان يرسل رسالة انتهاء الجولة مرة واحدة، ثم لا يخبرك إذا اكتملت بيانات العدل بعد ثوانٍ.

## ما تم تعديله

### collector.py

- التقاط Request/Response HTTP.
- حفظ WebSocket frames الواردة والصادرة.
- مراقبة JSON/text/javascript/html/xml من نطاق اللعبة وإطاراتها.
- حقن مراقبة `fetch` و`XMLHttpRequest` إلى جانب WebSocket.
- فحص كل iframe.
- فحص `innerText` وHTML وattributes وmeta وscripts وlocalStorage وsessionStorage.
- إضافة `diagnostics.jsonl`.
- حفظ مصدر كل حقل Seed يتم العثور عليه.
- ربط Server Seed المتأخرة مباشرة بالجولة المكتملة السابقة.
- إعادة حساب التحقق بعد إثراء الجولة.
- عدم إنشاء أي Seed أو Hash اصطناعي.

### seeds.py

- تحسين تصنيف أسماء الحقول.
- تقليل false positives الناتجة من أي قيمة hex عشوائية.
- إضافة استخراج من HTML attributes.
- تحسين قراءة `Seed Hash` و`Server Seed` و`Client Seed` و`Nonce` من النص.

### bot.py

- تغيرت رسالة الحالة بحيث لا تقول إن Server Seed ستظهر حتمًا إذا لم يتم التقاطها.
- إضافة إشعار Telegram لتحديث بيانات العدل إذا وصلت بعد انتهاء الجولة.
- عرض مصدر التقاط الحقول عند توفره.

### store.py

- إضافة سجل `diagnostics.jsonl`.
- الاستمرار في تحديث نفس الجولة بدل إنشاء سجل منفصل عند وصول بيانات متأخرة.

### config.json

تم حذف Bot Token المكشوف ووضع:

`PUT_NEW_BOT_TOKEN_HERE`

## ما لم يتم تغييره

- `predictor.py` لم يتغير.
- `run_bot.bat` لم يتغير.
- لا توجد أوامر رهان أو سحب.
- Workspace/PWA الخاص بـ Grok ليس جزءًا من مصدر البيانات الحقيقي.

## الاختبارات

تم تشغيل `py_compile` على ملفات Python.
تم تشغيل `test_fairness.py` بنجاح.
تم اختبار:

- استخراج الحقول من نص Provably-Fair.
- استخراج attributes من HTML.
- تجاهل OnCrash الحقيقي عندما لا يحتوي أي Seed.
- ربط Server Seed المتأخرة بالجولة السابقة.
- عدم نقل Server Seed المتأخرة إلى الجولة الجديدة.


### إصلاح Telegram في الإصدار التالي
- إصلاح بدء `history-monitor` باستخدام `asyncio.create_task` لإزالة تحذير `Application.create_task` في `post_init`.
- إضافة تسجيل واضح لعدد مشتركي Telegram عند بدء التشغيل.
- إضافة `/test` لاختبار Telegram وتسجيل chat id تلقائيًا.
- عدم حذف المشترك بسبب أخطاء الشبكة/API المؤقتة.
- تسجيل أخطاء Telegram في `telegram_errors.log`.
- إضافة عدد مشتركي Telegram إلى `/status`.
