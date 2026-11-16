"""Optional starter set from the project spec. Nothing here loads unless an administrator presses the button in
System Settings, so the database still starts clean. Everything loaded stays fully editable."""
from .models import Category, FieldDefinition, Subtype

SUBTYPES = {
    "Chillers": [("Air-Cooled Screw Chillers", "مبردات لولبية مبردة بالهواء"), ("Air-Cooled Scroll Chillers", "مبردات حلزونية مبردة بالهواء"),
                 ("Water-Cooled Screw Chillers", "مبردات لولبية مبردة بالماء"), ("Water-Cooled Centrifugal Chillers", "مبردات طاردة مركزية مبردة بالماء"),
                 ("Modular Chillers", "مبردات معيارية"), ("Heat Recovery Chillers", "مبردات استرداد الحرارة")],
    "Air Handling Units (AHU)": [("Fresh Air AHU", "وحدة هواء نقي"), ("Return Air AHU", "وحدة هواء راجع"), ("Mixing AHU", "وحدة خلط الهواء"),
                                 ("Heat Recovery AHU", "وحدة استرداد الحرارة"), ("Hygienic AHU", "وحدة صحية")],
    "Fan Coil Units (FCU)": [("Concealed FCU", "وحدة مخفية"), ("Cassette FCU", "وحدة كاسيت"), ("Wall-Mounted FCU", "وحدة جدارية"),
                             ("Floor-Mounted FCU", "وحدة أرضية"), ("Ducted FCU", "وحدة بمجاري هواء")],
}

# name_en: (name_ar, type, units, options)
F = {
    "Cooling Capacity": ("سعة التبريد", "measurement", "kW, TR", ""),
    "Heating Capacity": ("سعة التدفئة", "measurement", "kW, BTU/h", ""),
    "Sensible Cooling Capacity": ("سعة التبريد المحسوسة", "measurement", "kW", ""),
    "Cooling Type": ("نوع التبريد", "select", "", "Air-Cooled\nWater-Cooled"),
    "Refrigerant Type": ("نوع وسيط التبريد", "text", "", ""),
    "Compressor Type": ("نوع الضاغط", "text", "", ""),
    "Number of Compressors": ("عدد الضواغط", "integer", "", ""),
    "Number of Refrigerant Circuits": ("عدد دوائر التبريد", "integer", "", ""),
    "Electrical Supply": ("التغذية الكهربائية", "text", "", ""),
    "Voltage": ("الجهد", "measurement", "V", ""),
    "Frequency": ("التردد", "measurement", "Hz", ""),
    "Water Flow Rate": ("معدل تدفق الماء", "measurement", "l/s, m³/h", ""),
    "Water Inlet Temperature": ("درجة حرارة دخول الماء", "measurement", "°C", ""),
    "Water Outlet Temperature": ("درجة حرارة خروج الماء", "measurement", "°C", ""),
    "Dimensions": ("الأبعاد", "text", "", ""),
    "Weight": ("الوزن", "measurement", "kg", ""),
    "Controller Type": ("نوع وحدة التحكم", "text", "", ""),
    "Manufacturing Year": ("سنة التصنيع", "integer", "", ""),
    "Airflow": ("تدفق الهواء", "measurement", "m³/h, CFM, l/s", ""),
    "External Static Pressure": ("الضغط الساكن الخارجي", "measurement", "Pa", ""),
    "Power Supply": ("مصدر الطاقة", "text", "", ""),
    "Fan Type": ("نوع المروحة", "text", "", ""),
    "Filter Type": ("نوع الفلتر", "text", "", ""),
    "Heating System": ("نظام التدفئة", "text", "", ""),
    "Supply Airflow": ("تدفق هواء التغذية", "measurement", "m³/h, CFM, l/s", ""),
    "Return Airflow": ("تدفق الهواء الراجع", "measurement", "m³/h, CFM, l/s", ""),
    "Fresh Airflow": ("تدفق الهواء النقي", "measurement", "m³/h, CFM, l/s", ""),
    "Cooling Coil Capacity": ("سعة ملف التبريد", "measurement", "kW", ""),
    "Heating Coil Capacity": ("سعة ملف التدفئة", "measurement", "kW", ""),
    "Filter Stages": ("مراحل الفلترة", "integer", "", ""),
    "Filter Class": ("فئة الفلتر", "text", "", ""),
    "Motor Power": ("قدرة المحرك", "measurement", "kW", ""),
    "Heat Recovery Type": ("نوع استرداد الحرارة", "text", "", ""),
    "Coil Rows": ("صفوف الملف", "integer", "", ""),
    "Fan Speed": ("سرعة المروحة", "text", "", ""),
    "Coil Configuration": ("تكوين الملف", "text", "", ""),
    "Installation Type": ("نوع التركيب", "text", "", ""),
    "Humidity Control": ("التحكم بالرطوبة", "boolean", "", ""),
    "Refrigerant": ("وسيط التبريد", "text", "", ""),
}

FIELDS = {
    "Chillers": ["Cooling Capacity", "Cooling Type", "Refrigerant Type", "Compressor Type", "Number of Compressors", "Number of Refrigerant Circuits",
                 "Electrical Supply", "Voltage", "Frequency", "Water Flow Rate", "Water Inlet Temperature", "Water Outlet Temperature", "Dimensions", "Weight",
                 "Controller Type", "Manufacturing Year"],
    "Rooftop Units (RTU)": ["Cooling Capacity", "Heating Capacity", "Airflow", "External Static Pressure", "Compressor Type", "Number of Compressors", "Refrigerant",
                            "Power Supply", "Fan Type", "Filter Type", "Heating System", "Controller Type"],
    "Air Handling Units (AHU)": ["Supply Airflow", "Return Airflow", "Fresh Airflow", "External Static Pressure", "Cooling Coil Capacity", "Heating Coil Capacity",
                                 "Filter Stages", "Filter Class", "Fan Type", "Motor Power", "Heat Recovery Type", "Coil Rows", "Water Flow Rate", "Dimensions", "Controller Type"],
    "Fan Coil Units (FCU)": ["Cooling Capacity", "Heating Capacity", "Airflow", "Fan Speed", "Water Flow Rate", "Coil Configuration", "Installation Type", "Electrical Supply", "Controller Type"],
    "Close Control Units (CCU)": ["Cooling Capacity", "Sensible Cooling Capacity", "Airflow", "Humidity Control", "Refrigerant", "Compressor Type", "Electrical Supply", "Fan Type", "Controller Type"],
}


def load_suggested():
    """Adds only what is missing, so pressing it twice or after customizing changes nothing that exists."""
    added = 0
    for cat_name, subs in SUBTYPES.items():
        cat = Category.objects.filter(name_en=cat_name).first()
        if cat:
            for en, ar in subs:
                added += Subtype.objects.get_or_create(category=cat, name_en=en, defaults={"name_ar": ar})[1]
    for cat_name, names in FIELDS.items():
        cat = Category.objects.filter(name_en=cat_name).first()
        if not cat:
            continue
        for order, name in enumerate(names, 1):
            ar, kind, units, options = F[name]
            added += FieldDefinition.objects.get_or_create(category=cat, subtype=None, name_en=name, defaults={
                "name_ar": ar, "field_type": kind, "units": units, "options": options, "sort_order": order})[1]
    return added
