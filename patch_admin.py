import re

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Add "功能开关" to the nav
nav_button = """            <button @click="currentTab = 'toggles'; fetchFeatureToggles()" :class="{'border-b-2 border-[#4199a0] text-[#4199a0]': currentTab === 'toggles', 'text-[#5a7a7d] hover:text-[#5a7a7d]': currentTab !== 'toggles'}" class="px-4 py-3 text-sm font-medium whitespace-nowrap transition-colors">功能开关</button>"""
html = html.replace('<button @click="currentTab = \'rules\'"', nav_button + '\n            <button @click="currentTab = \'rules\'"')

# 2. Add the section to the main
section_html = """
            <!-- 功能开关 -->
            <section v-show="currentTab === 'toggles'">
                <div class="mb-4">
                    <h2 class="text-lg font-semibold text-[#2c3e50]">功能开关</h2>
                </div>

                <!-- 网关总开关 (单独大卡片) -->
                <div class="mb-6 bg-white rounded-xl p-5 border border-[#aecfd1] shadow-sm flex items-center justify-between">
                    <div>
                        <h3 class="font-bold text-lg text-[#2c3e50]">网关总开关</h3>
                        <p class="text-sm text-[#77b5b4] mt-1">关闭后，网关将退化为纯粹的反向代理，不提供任何提示词注入、记忆或对话总结功能。</p>
                    </div>
                    <div class="flex items-center">
                        <div @click="toggleFeature('gateway_master_switch', !featureToggles.gateway_master_switch)" class="relative w-14 h-7 rounded-full transition-colors duration-300 cursor-pointer" :class="featureToggles.gateway_master_switch ? 'bg-emerald-500' : 'bg-slate-300'">
                            <div class="absolute top-1 w-5 h-5 rounded-full bg-white shadow-md transition-all duration-300" :class="featureToggles.gateway_master_switch ? 'left-8' : 'left-1'"></div>
                        </div>
                    </div>
                </div>

                <!-- 子功能开关列表 -->
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div v-for="item in featureToggleItems" :key="item.key" class="bg-white rounded-xl p-4 border border-[#aecfd1]/50 shadow-sm flex items-center justify-between" :class="{'opacity-50': !featureToggles.gateway_master_switch}">
                        <div class="pr-4">
                            <h4 class="font-medium text-[#2c3e50]">{{ item.name }}</h4>
                            <p class="text-xs text-[#77b5b4] mt-1">{{ item.desc }}</p>
                        </div>
                        <div class="flex items-center shrink-0">
                            <div @click="featureToggles.gateway_master_switch && toggleFeature(item.key, !featureToggles[item.key])" class="relative w-11 h-6 rounded-full transition-colors duration-300" :class="[!featureToggles.gateway_master_switch ? 'cursor-not-allowed bg-slate-200' : 'cursor-pointer', featureToggles[item.key] ? 'bg-[#4199a0]' : 'bg-slate-300']">
                                <div class="absolute top-0.5 w-5 h-5 rounded-full bg-white shadow-md transition-all duration-300" :class="featureToggles[item.key] ? 'left-[22px]' : 'left-0.5'"></div>
                            </div>
                        </div>
                    </div>
                </div>
            </section>
"""
html = html.replace('<!-- 提示词注入规则管理 -->', section_html + '\n            <!-- 提示词注入规则管理 -->')

# 3. Add state and methods
state_inject = """
                const featureToggles = reactive({
                    gateway_master_switch: true,
                    feature_injection: true,
                    feature_memory: true,
                    feature_summary: true,
                    feature_qq: true,
                    feature_health: true,
                    feature_music: true
                });

                const featureToggleItems = [
                    { key: 'feature_injection', name: '提示词注入', desc: '包含时间、天气、系统设定的动态注入' },
                    { key: 'feature_memory', name: '记忆存取', desc: '长短期记忆的自动提取与召回' },
                    { key: 'feature_summary', name: '对话总结', desc: '按固定条数或时间周期生成聊天记录总结' },
                    { key: 'feature_qq', name: 'QQ 桥接', desc: '处理通过 NapCat 转发的 QQ 消息' },
                    { key: 'feature_health', name: '健康状态', desc: '小米运动健康手环数据轮询' },
                    { key: 'feature_music', name: '音乐状态', desc: '网易云一起听播放状态检测' }
                ];

                const fetchFeatureToggles = async () => {
                    try {
                        const res = await fetch('/admin/api/config');
                        const data = await res.json();
                        Object.keys(featureToggles).forEach(key => {
                            if (data[key]) {
                                featureToggles[key] = data[key].value === '1';
                            }
                        });
                    } catch (e) {
                        showToast('无法获取功能开关配置', 'error');
                    }
                };

                const toggleFeature = async (key, val) => {
                    featureToggles[key] = val; // 乐观更新
                    try {
                        await fetch('/admin/api/config', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ key: key, value: val ? '1' : '0' })
                        });
                        showToast('配置已保存');
                    } catch (e) {
                        featureToggles[key] = !val; // 恢复
                        showToast('保存失败', 'error');
                    }
                };
"""
html = html.replace('// 记忆总览状态', state_inject + '\n                // 记忆总览状态')

# 4. Add returns
returns_inject = """                    featureToggles, featureToggleItems, fetchFeatureToggles, toggleFeature,"""
html = html.replace('currentTab,', returns_inject + '\n                    currentTab,')

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'w', encoding='utf-8') as f:
    f.write(html)
    
print("admin.html patched successfully")
