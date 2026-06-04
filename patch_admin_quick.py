with open('/home/ubuntu/caeron-gateway/static/admin.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Fix the toggleFeature method to use PUT instead of POST, and check res.ok
bad_toggle = """                        await fetch('/admin/api/config', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ key: key, value: val ? '1' : '0' })
                        });"""

good_toggle = """                        const res = await fetch('/admin/api/config/' + key, {
                            method: 'PUT',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ value: val ? '1' : '0' })
                        });
                        if (!res.ok) throw new Error('API Error');"""

html = html.replace(bad_toggle, good_toggle)

# Fix the fetchFeatureToggles logic to handle Array instead of Map
bad_fetch = """                        Object.keys(featureToggles).forEach(key => {
                            if (data[key]) {
                                featureToggles[key] = data[key].value === '1';
                            }
                        });"""

good_fetch = """                        data.forEach(item => {
                            if (featureToggles[item.key] !== undefined) {
                                featureToggles[item.key] = item.value === '1';
                            }
                        });"""

html = html.replace(bad_fetch, good_fetch)

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'w', encoding='utf-8') as f:
    f.write(html)
    
print("Quick patch applied to admin.html successfully")
