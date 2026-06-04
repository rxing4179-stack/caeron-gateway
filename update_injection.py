import sqlite3
conn = sqlite3.connect('/home/ubuntu/caeron-gateway/caeron.db')
c = conn.cursor()
c.execute('SELECT content FROM injection_rules WHERE name LIKE "%常驻%"')
old = c.fetchone()[0]
new_section = '\n\n#### 服务器环境配置\n- 服务器 IP: 1.14.59.116 (Ubuntu 22.04)\n- 用户名: ubuntu, 密码: DGBDdgbd2005\n- Gateway: ~/caeron-gateway (端口 8080, 数据库 caeron.db)\n- NapCat: ~/napcat (Docker 容器, QQ号 3621487982)\n- WebSocket: ws://127.0.0.1:8080/onebot/ws\n- 音乐监控: 网易云实时同步\n- 健康数据: 小米手环 API (已配置但禁用)'
if '#### 服务器环境配置' not in old:
    updated = old + new_section
    c.execute('UPDATE injection_rules SET content=? WHERE name LIKE "%常驻%"', (updated,))
    conn.commit()
    print('✓ 更新成功，新长度:', len(updated))
else:
    print('✗ 已存在服务器配置段')
conn.close()
