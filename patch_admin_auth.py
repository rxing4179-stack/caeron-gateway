patch_js = '''<script>
(function(){
  var token = localStorage.getItem('caeron_admin_token');
  if (!token) {
    token = prompt('Caeron Admin Token:');
    if (token) localStorage.setItem('caeron_admin_token', token);
  }
  if (token) {
    var _fetch = window.fetch;
    window.fetch = function(url, opts) {
      if (typeof url === 'string' && url.indexOf('/admin/api/') !== -1) {
        opts = opts || {};
        opts.headers = opts.headers || {};
        if (opts.headers instanceof Headers) {
          opts.headers.set('Authorization', 'Bearer ' + token);
        } else {
          opts.headers['Authorization'] = 'Bearer ' + token;
        }
      }
      return _fetch.call(this, url, opts);
    };
  }
  window.__caeronLogout = function(){ localStorage.removeItem('caeron_admin_token'); location.reload(); };
})();
</script>'''

html_path = '/home/ubuntu/caeron-gateway/static/admin.html'
with open(html_path, 'r') as f:
    content = f.read()

if 'caeron_admin_token' in content:
    print('ALREADY PATCHED - skipping')
else:
    content = content.replace('<head>', '<head>\n' + patch_js, 1)
    with open(html_path, 'w') as f:
        f.write(content)
    print('PATCH OK - auth fetch wrapper injected')
