#!/usr/bin/env python3
import re

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'r') as f:
    content = f.read()

start_marker = '<!-- 对话记录 -->'
end_marker = '</section>\n        </main>'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker, start_idx)

if start_idx < 0 or end_idx < 0:
    print(f'ERROR: markers not found start={start_idx} end={end_idx}')
    exit(1)

new_section = '''<!-- 对话记录 -->
            <section v-show="currentTab === 'conversations'">
                <!-- 顶部工具栏 -->
                <div class="mb-4 space-y-3">
                    <div class="flex justify-between items-center">
                        <h2 class="text-lg font-semibold text-[#2c3e50]">对话记录</h2>
                        <button @click="fetchConversations" class="text-[#5a7a7d] hover:text-[#4199a0] p-1" title="刷新">
                            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                        </button>
                    </div>

                    <!-- 搜索框 -->
                    <div class="relative">
                        <input v-model="searchQuery" @input="debounceSearch" type="text" placeholder="搜索消息内容..."
                               class="w-full bg-white border border-[#aecfd1] rounded-lg pl-9 pr-8 py-2 text-sm text-[#2c3e50] focus:outline-none focus:border-[#4199a0] focus:ring-1 focus:ring-[#4199a0]">
                        <svg class="absolute left-3 top-2.5 w-4 h-4 text-[#77b5b4]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                        <button v-if="searchQuery" @click="searchQuery = ''; searchResults = []" class="absolute right-3 top-2.5 text-[#77b5b4] hover:text-[#4199a0]">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                        </button>
                    </div>

                    <!-- 时间筛选 -->
                    <div class="flex gap-2 items-center text-sm flex-wrap">
                        <span class="text-[#5a7a7d] shrink-0 text-xs">时间:</span>
                        <input v-model="dateStart" @change="fetchConversations" type="date" class="bg-white border border-[#aecfd1] rounded-lg px-2 py-1 text-xs text-[#2c3e50] focus:outline-none focus:border-[#4199a0]">
                        <span class="text-[#77b5b4] text-xs">至</span>
                        <input v-model="dateEnd" @change="fetchConversations" type="date" class="bg-white border border-[#aecfd1] rounded-lg px-2 py-1 text-xs text-[#2c3e50] focus:outline-none focus:border-[#4199a0]">
                        <button v-if="dateStart || dateEnd" @click="dateStart = ''; dateEnd = ''; fetchConversations()" class="text-[10px] text-[#77b5b4] hover:text-[#4199a0] underline">清除</button>
                    </div>
                </div>

                <!-- 搜索结果区 -->
                <div v-if="searchResults.length > 0" class="mb-4">
                    <h3 class="text-sm font-medium text-[#5a7a7d] mb-2">搜索结果 ({{ searchResults.length }}条)</h3>
                    <div class="space-y-2">
                        <div v-for="sr in searchResults" :key="sr.id" class="bg-white rounded-lg border border-[#aecfd1] p-3">
                            <div class="flex items-center gap-2 mb-1">
                                <span class="text-[10px] font-mono bg-[#4199a0]/20 text-[#4199a0] px-1 py-0.5 rounded">{{ sr.conversation_id.slice(0, 8) }}</span>
                                <span class="text-[10px] px-1.5 py-0.5 rounded" :class="sr.role === 'user' ? 'bg-[#4199a0] text-white' : 'bg-[#aecfd1] text-[#5a7a7d]'">{{ sr.role }}</span>
                                <span class="text-[10px] text-[#77b5b4]">{{ sr.model || '' }}</span>
                                <span class="text-[10px] text-[#77b5b4] ml-auto">{{ formatTime(sr.created_at) }}</span>
                            </div>
                            <p class="text-sm text-[#2c3e50] whitespace-pre-wrap break-words" v-html="highlightSearch(sr.snippet)"></p>
                        </div>
                    </div>
                </div>
                <div v-else-if="searchQuery && !loading.search" class="mb-4 text-center py-4 text-sm text-[#77b5b4]">
                    无搜索结果
                </div>
                <div v-if="loading.search" class="mb-4 text-center py-4 text-sm text-[#77b5b4]">搜索中...</div>

                <!-- 对话列表 -->
                <div v-show="!searchQuery" class="space-y-3">
                    <div v-if="loading.conversations" class="text-center py-8 text-[#77b5b4]">加载中...</div>
                    <div v-else-if="conversations.length === 0" class="text-center py-8 text-[#77b5b4] bg-white/50 rounded-xl border border-[#aecfd1]/50">
                        暂无对话记录
                    </div>

                    <div v-for="conv in conversations" :key="conv.conversation_id" class="bg-white rounded-xl border border-[#aecfd1] overflow-hidden">
                        <!-- 对话卡片头部 -->
                        <div class="p-4 cursor-pointer hover:bg-[#deebec]/50 transition-colors" @click="toggleConversation(conv)">
                            <div class="flex justify-between items-start">
                                <div class="flex-1 min-w-0 mr-3">
                                    <div class="flex items-center gap-2 mb-1">
                                        <span class="text-[10px] bg-[#4199a0]/20 text-[#4199a0] px-1.5 py-0.5 rounded font-mono">{{ conv.conversation_id.slice(0, 8) }}</span>
                                        <span class="text-[10px] bg-[#aecfd1] text-[#5a7a7d] px-1.5 py-0.5 rounded">{{ conv.message_count }} 条</span>
                                    </div>
                                    <p class="text-sm text-[#2c3e50] truncate">{{ conv.preview || '(空消息)' }}</p>
                                    <div class="flex items-center gap-3 mt-1 text-[10px] text-[#77b5b4]">
                                        <span>模型: {{ conv.model || '未知' }}</span>
                                        <span>{{ formatTime(conv.last_message_at || conv.created_at) }}</span>
                                    </div>
                                </div>
                                <div class="flex items-center gap-1">
                                    <button @click.stop="deleteConversation(conv.conversation_id)" class="text-[#77b5b4] hover:text-red-400 p-1" title="删除">
                                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                                    </button>
                                    <svg class="w-4 h-4 text-[#77b5b4] transition-transform" :class="{'rotate-180': conv._expanded}" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path></svg>
                                </div>
                            </div>
                        </div>

                        <!-- 展开的消息列表 -->
                        <div v-if="conv._expanded" class="border-t border-[#aecfd1] bg-[#f7fafa] max-h-[60vh] overflow-y-auto hide-scrollbar">
                            <div v-if="conv._loadingMessages" class="text-center py-6 text-[#77b5b4] text-sm">加载消息中...</div>
                            <div v-else class="p-3 space-y-2">
                                <template v-for="msg in conv._messages" :key="msg.id">

                                    <!-- 对话摘要 -->
                                    <div v-if="isSummary(msg)" class="flex flex-col items-center">
                                        <button @click="msg._showDetail = !msg._showDetail" class="text-[10px] bg-[#e8d5b7]/60 text-[#8b7355] px-3 py-1 rounded-full hover:bg-[#e8d5b7]">
                                            对话摘要 {{ msg._showDetail ? '(收起)' : '(点击展开)' }}
                                        </button>
                                        <div v-if="msg._showDetail" class="mt-2 w-full bg-[#fdf6eb]/70 border border-[#e8d5b7]/50 rounded-lg p-3 text-[10px] text-[#8b7355] font-mono whitespace-pre-wrap break-all max-h-[200px] overflow-y-auto">
                                            {{ msg.content.slice(0, 2000) }}{{ msg.content.length > 2000 ? '\n...(已截断)' : '' }}
                                        </div>
                                    </div>

                                    <!-- system消息 -->
                                    <div v-else-if="msg.role === 'system'" class="flex flex-col items-center">
                                        <button @click="msg._showDetail = !msg._showDetail" class="text-[10px] bg-[#aecfd1]/30 text-[#5a7a7d] px-3 py-1 rounded-full hover:bg-[#aecfd1]/50">
                                            [系统提示] {{ msg._showDetail ? '收起' : '展开' }}
                                        </button>
                                        <div v-if="msg._showDetail" class="mt-2 w-full bg-[#aecfd1]/10 border border-[#aecfd1]/30 rounded-lg p-3 text-[10px] text-[#5a7a7d] font-mono whitespace-pre-wrap break-all max-h-[200px] overflow-y-auto">
                                            {{ msg.content?.slice(0, 1000) }}{{ msg.content?.length > 1000 ? '...' : '' }}
                                        </div>
                                    </div>

                                    <!-- 空assistant / 工具调用 -->
                                    <div v-else-if="msg.role === 'assistant' && isEmptyOrToolCall(msg)" class="flex justify-center">
                                        <span class="text-[10px] bg-amber-100/70 text-amber-600 px-2 py-0.5 rounded-full">工具调用</span>
                                    </div>

                                    <!-- user消息 -->
                                    <div v-else-if="msg.role === 'user'" class="flex flex-col items-end">
                                        <div class="max-w-[85%]">
                                            <div class="bg-[#4199a0] text-white rounded-2xl rounded-br-md px-3 py-2 text-sm whitespace-pre-wrap break-words">
                                                {{ getUserText(msg.content) }}
                                            </div>
                                            <div v-if="hasAttachment(msg.content)" class="mt-1 text-right">
                                                <button @click="msg._showDetail = !msg._showDetail" class="text-[10px] text-[#77b5b4] hover:text-[#4199a0]">
                                                    附加信息 {{ msg._showDetail ? '(收起)' : '(展开)' }}
                                                </button>
                                                <div v-if="msg._showDetail" class="mt-1 bg-[#aecfd1]/10 border border-[#aecfd1]/30 rounded-lg p-2 text-[10px] text-[#5a7a7d] font-mono whitespace-pre-wrap break-all max-h-[150px] overflow-y-auto text-left">
                                                    {{ getAttachmentText(msg.content) }}
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    <!-- assistant消息 -->
                                    <div v-else-if="msg.role === 'assistant'" class="flex flex-col items-start">
                                        <div class="max-w-[85%]">
                                            <div v-if="hasThinking(msg.content)" class="mb-1">
                                                <button @click="msg._showThinking = !msg._showThinking" class="text-[10px] bg-purple-100/70 text-purple-500 px-2 py-0.5 rounded-full hover:bg-purple-100">
                                                    思考过程 {{ msg._showThinking ? '(收起)' : '(展开)' }}
                                                </button>
                                                <div v-if="msg._showThinking" class="mt-1 bg-purple-50/70 border border-purple-200/50 rounded-lg p-2 text-[10px] text-purple-700 whitespace-pre-wrap break-words max-h-[200px] overflow-y-auto">
                                                    {{ getThinking(msg.content) }}
                                                </div>
                                            </div>
                                            <div v-if="getAssistantText(msg.content)" class="bg-white border border-[#aecfd1] rounded-2xl rounded-bl-md px-3 py-2 text-sm text-[#2c3e50] whitespace-pre-wrap break-words">
                                                {{ getAssistantText(msg.content).slice(0, 800) }}{{ getAssistantText(msg.content).length > 800 ? '\n...(已截断)' : '' }}
                                            </div>
                                        </div>
                                    </div>

                                </template>
                            </div>
                        </div>
                    </div>
                </div>
            </section>
        </main>'''

# Replace from start_marker to end_marker (inclusive of end_marker)
old_section = content[start_idx:end_idx + len(end_marker)]
new_content = content[:start_idx] + new_section + content[end_idx + len(end_marker):]

with open('/home/ubuntu/caeron-gateway/static/admin.html', 'w') as f:
    f.write(new_content)

print(f'SUCCESS: replaced {len(old_section)} chars with {len(new_section)} chars')
