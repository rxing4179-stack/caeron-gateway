with open('/home/ubuntu/caeron-gateway/static/admin.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Fix the method and URL in toggleFeature
html = html.replace(
    "await fetch('/admin/api/config', {", 
    "await fetch('/admin/api/config/' + key, {"
)
html = html.replace(
    "method: 'POST',", 
    "method: 'PUT',"
)
html = html.replace(
    "body: JSON.stringify({ key: key, value: val ? '1' : '0' })", 
    "body: JSON.stringify({ value: val ? '1' : '0' })"
)

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("admin.html API endpoint patched")
