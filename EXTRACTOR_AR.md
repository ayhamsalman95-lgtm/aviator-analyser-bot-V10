# أداة استخراج أدلة الشبكة

## النظرة العامة

`tools/extract_network_evidence.py` هي أداة قائمة بذاتها لاستخراج أدلة الإنصاف والرسائل والإطارات من ملفات `game_network.jsonl` الخارجية بدون الحاجة إلى التزام الملف الخام بـ GitHub.

**الميزات:**
- معالجة ملفات تصل إلى 117 ميغابايت بكفاءة
- استخراج الأدلة المشفرة (البذور، الالتزامات، الرموز)
- تصنيف رسائل SmartFox (roundChartInfo, changeState, إلخ)
- استخراج إطارات WebSocket ذات الصلة
- توليد ملف أدلة مشتق أصغر حجماً
- دعم ملفات JSONL العادية والمضغوطة بـ gzip

## الاستخدام

### من سطر الأوامر

```bash
# الاستخدام الأساسي
python tools/extract_network_evidence.py game_network.jsonl

# مع ملف إخراج مخصص
python tools/extract_network_evidence.py game_network.jsonl --output derived_evidence.jsonl

# من ملف مضغوط
python tools/extract_network_evidence.py game_network.jsonl.gz --output evidence.jsonl
```

### من Windows Batch

```batch
extract_network_evidence.bat D:\game_network.jsonl --output D:\evidence.jsonl
```

## المخرجات

### ملف الأدلة المشتق

يحتوي `derived_evidence.jsonl` على سجلات JSON مفصولة بأسطر، كل واحدة منها تمثل:

#### 1. أدلة الإنصاف (Fairness Evidence)
```json
{
  "kind": "fairness_evidence",
  "timestamp": 1000000.5,
  "source": "network",
  "path": "$.params.fairness",
  "round_id": 5001,
  "server_seed": "abc123def456...",
  "player_seeds": ["seed1", "seed2"],
  "commitment": "abcdef0123456789abcdef0123456789",
  "round_hash": "abcdef0123456789..."
}
```

#### 2. رسائل SmartFox (SFS Messages)
```json
{
  "kind": "sfs:roundChartInfo",
  "timestamp": 1000001.0,
  "cmd": "roundChartInfo",
  "source": "sfs",
  "has_data": true
}
```

الأنواع المدعومة:
- `sfs:roundChartInfo` - بيانات الرسم البياني للجولة
- `sfs:changeState` - تغيير حالة اللعبة
- `sfs:init.roundsInfo` - معلومات الجولات الأولية
- `sfs:serverSeedResponse` - استجابة البذرة المرجعية
- `sfs:roundFairnessResponse` - استجابة إنصاف الجولة
- `sfs_message` - رسالة SmartFox عامة

#### 3. إطارات WebSocket (WebSocket Frames)
```json
{
  "kind": "websocket_frame",
  "timestamp": 1000002.0,
  "frame_type": "binary",
  "source": "websocket",
  "has_data": true
}
```

### تقرير الملخص

تطبع الأداة تقريراً يحتوي على:
- عدد الأسطر الإجمالي ووقت المعالجة
- عدد الأخطاء في JSON
- توزيع أنواع الأدلة المستخرجة
- حجم ملف الإخراج

مثال:
```
=== Network Evidence Extraction Summary ===
Source: game_network.jsonl
Output: derived_evidence.jsonl
Time: 45.23s

Input statistics:
  Total lines: 1,245,600
  Parsed: 1,245,598
  Errors: 2

Extracted records:
  Fairness evidence: 12,450
  SFS messages (total): 45,230
    - roundChartInfo: 12,340
    - changeState: 18,560
    - init.roundsInfo: 8,120
    - serverSeedResponse: 4,210
    - roundFairnessResponse: 2,000
  WebSocket frames: 8,920
  Total records written: 66,600

Output file size: 8.45 MB
```

## التفاصيل التقنية

### منطق الاستخراج

1. **أدلة الإنصاف**: تستخدم منطق `aviator.extract.extract_fairness()` الموجود
   - تبحث عن المفاتيح المحددة بوضوح فقط (serverSeed, playerSeeds, إلخ)
   - لا تقوم بمطابقة الكلمات الرئيسية (لا تعتمد على نص يحتوي "hash" أو "seed")
   - تربط الأدلة برقم الجولة فقط عندما يكون الاثنان في نفس الكائن

2. **تصنيف رسائل SFS**:
   - تحدد الأنواع الأساسية أولاً (roundChartInfo, changeState, إلخ)
   - تستخرج البيانات الأساسية دون الحمولات الثنائية الكاملة
   - تفادي تصنيف JavaScript/HTML بناءً على وجود كلمات مثل "SmartFox"

3. **عدم الالتزام**:
   - الملف الخام `game_network.jsonl` لم يتم تعديله مطلقاً
   - ملف الإخراج `derived_evidence.jsonl` أصغر بكثير ومناسب للتحكم بالإصدارات

## الأداء

- **معدل المعالجة**: ~25,000 سطر/ثانية على معالج حديث
- **الذاكرة**: ~50 ميغابايت للملفات الكبيرة (بدون تحميل الملف بالكامل)
- **حجم الإخراج**: عادة 10-15% من حجم الإدخال

## معالجة الأخطاء

- سطور JSON غير صحيحة: يتم تخطيها مع حساب الخطأ
- طوابع زمنية مفقودة: تستخدم الوقت الحالي كقيمة افتراضية
- ملفات غير موجودة: رسالة خطأ واضحة والخروج برمز 1

## الاختبارات

```bash
# تشغيل جميع اختبارات المستخرج
python -m pytest tests/test_network_extractor.py -v

# اختبارات محددة
python -m pytest tests/test_network_extractor.py::NetworkExtractorTests::test_extract_fairness_evidence -v
```

## الدمج مع سير العمل

```bash
# 1. جمع بيانات الشبكة من الكروم (140 ساعة لعب)
python -m scripts.collect_and_validate

# 2. استخراج الأدلة من ملف الشبكة الخام
python tools/extract_network_evidence.py logs/network/game_network.jsonl \
  --output logs/network/derived_evidence.jsonl

# 3. استخدام الأدلة في إعادة بناء مصدقة
python -m scripts.verify_fairness logs/network/derived_evidence.jsonl
```

## الأسئلة الشائعة

**س: ماذا لو كان ملف الشبكة بحجم 150 ميغابايت؟**
ج: الأداة تعالج السطور تسلسلياً دون تحميل الملف بالكامل، لذا الحد الوحيد هو الذاكرة المتاحة.

**س: هل يمكن تشغيل الأداة على ملفات متعددة؟**
ج: نعم، استخدم حلقة bash أو اكتب سكريبت wrapper.

**س: هل يتم حفظ البيانات الحساسة في الملف المشتق؟**
ج: لا، يتم حفظ البذور والالتزامات فقط (البيانات العلنية من Aviator)، بدون كلمات مرور أو رموز.

**س: كيف أتحقق من سلامة الاستخراج؟**
ج: قارن حقول السجل في `derived_evidence.jsonl` مع الملف الخام باستخدام `jq` أو Python.
