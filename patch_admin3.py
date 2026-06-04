with open('/home/ubuntu/caeron-gateway/static/admin.html', 'r', encoding='utf-8') as f:
    html = f.read()

bad_logic = """                        Object.keys(featureToggles).forEach(key => {
                            if (data[key]) {
                                featureToggles[key] = data[key].value === '1';
                            }
                        });"""

good_logic = """                        data.forEach(item => {
                            if (featureToggles[item.key] !== undefined) {
                                featureToggles[item.key] = item.value === '1';
                            }
                        });"""

html = html.replace(bad_logic, good_logic)

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("admin.html fetchFeatureToggles parsing logic patched")
