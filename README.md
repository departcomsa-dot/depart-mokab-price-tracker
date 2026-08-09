# Mokab vs Depart Price Tracker

برنامج يومي لمقارنة أسعار منتجات مكعب مع منتجات ديبارت عن طريق الباركود.

## الفكرة

- مكعب يتم قراءته من خريطة الموقع أو من Snapshot محلي كبداية.
- ديبارت يتم قراءته من ملف `data/depart_products.json` أو CSV بنفس الأعمدة.
- المطابقة الأساسية بالباركود/GTIN.
- إذا كان SKU في مكعب يحتوي أكثر من باركود، يعتبر المنتج باقة ويظهر كـ `bundle_contains` للمراجعة اليدوية.
- يتم إنشاء لوحة HTML في `docs/index.html` مناسبة لـ GitHub Pages.
- يتم إنشاء `issue_body.md` كملخص يومي يمكن إرساله كـ GitHub Issue، وبالتالي يصل على الإيميل عند تفعيل Watch للمستودع.
- يمكن تفعيل SMTP لاحقًا عبر GitHub Secrets لإرسال إيميل مباشر.

## الملفات المهمة

- `scripts/price_compare.py`: السكربت الرئيسي.
- `tracker_config.json`: إعدادات المصدر والمخرجات.
- `data/depart_products.json`: منتجات ديبارت الحالية.
- `data/mokab_snapshot.json`: Snapshot من منتجات مكعب كبداية.
- `docs/index.html`: لوحة العرض.
- `.github/workflows/daily-price-compare.yml`: التشغيل اليومي على GitHub Actions.

## التشغيل المحلي

```bash
pip install -r requirements.txt
python scripts/price_compare.py --mode snapshot
```

## التشغيل على GitHub

1. ارفع هذا المجلد كمستودع GitHub.
2. فعّل GitHub Pages من Settings → Pages واجعل المصدر `Deploy from a branch` ثم `main / docs`.
3. فعّل Actions.
4. أول تشغيل يبني baseline، وبعد ذلك سترى التغييرات اليومية.

## أسرار الإيميل الاختيارية

إذا أردت إرسال بريد مباشر بدل الاعتماد على GitHub Issues، أضف هذه الأسرار في GitHub:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `EMAIL_FROM`
- `EMAIL_TO`

بدون هذه الأسرار سيكتفي النظام بإنشاء GitHub Issue يومي.
