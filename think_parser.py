class ThinkTagParser:
    def __init__(self):
        self.in_think = False
        self.buffer = ""
        self.open_tags = ["<think>", "<thinking>", "<thinking_mode>", "<thought>", "<思考过程>", "<思考链>"]
        self.close_tags = ["</think>", "</thinking>", "</thinking_mode>", "</thought>", "</思考过程>", "</思考链>"]
        self.max_tag_len = max(len(t) for t in self.open_tags + self.close_tags)

    def process(self, chunk: str):
        self.buffer += chunk
        results = []
        while self.buffer:
            if not self.in_think:
                idx = self.buffer.find('<')
                if idx == -1:
                    results.append((False, self.buffer))
                    self.buffer = ""
                    break
                elif idx > 0:
                    results.append((False, self.buffer[:idx]))
                    self.buffer = self.buffer[idx:]
                else:
                    matched = False
                    for tag in self.open_tags:
                        if self.buffer.startswith(tag):
                            self.in_think = True
                            self.buffer = self.buffer[len(tag):]
                            matched = True
                            break
                    if matched: continue
                    
                    is_prefix = any(tag.startswith(self.buffer) for tag in self.open_tags)
                    if is_prefix and len(self.buffer) < self.max_tag_len:
                        break
                    else:
                        results.append((False, "<"))
                        self.buffer = self.buffer[1:]
            else:
                idx = self.buffer.find('<')
                if idx == -1:
                    results.append((True, self.buffer))
                    self.buffer = ""
                    break
                elif idx > 0:
                    results.append((True, self.buffer[:idx]))
                    self.buffer = self.buffer[idx:]
                else:
                    matched = False
                    for tag in self.close_tags:
                        if self.buffer.startswith(tag):
                            self.in_think = False
                            self.buffer = self.buffer[len(tag):]
                            matched = True
                            break
                    if matched: continue

                    # 守卫：若在思考块中检测到工具调用或执行标签，强制提前退出思考状态转入正文
                    if self.buffer.startswith("<tool_") or self.buffer.startswith("<tool ") or self.buffer.startswith("<tool>") or self.buffer.startswith("<exec"):
                        self.in_think = False
                        continue
                    
                    is_prefix = any(tag.startswith(self.buffer) for tag in self.close_tags)
                    if is_prefix and len(self.buffer) < self.max_tag_len:
                        break
                    else:
                        results.append((True, "<"))
                        self.buffer = self.buffer[1:]
        return results

class ReasoningStreamParser:
    def __init__(self):
        self.tool_diverted = False
        self.buffer = ""
        self.tool_prefixes = ["<tool_", "<tool ", "<tool>", "<exec ", "<exec>"]
        self.max_prefix_len = max(len(p) for p in self.tool_prefixes)

    def process(self, chunk: str):
        if self.tool_diverted:
            return [(False, chunk)]

        self.buffer += chunk
        results = []

        while self.buffer:
            idx = self.buffer.find('<')
            if idx == -1:
                results.append((True, self.buffer))
                self.buffer = ""
                break
            elif idx > 0:
                results.append((True, self.buffer[:idx]))
                self.buffer = self.buffer[idx:]

            matched = False
            for prefix in self.tool_prefixes:
                if self.buffer.startswith(prefix):
                    self.tool_diverted = True
                    results.append((False, self.buffer))
                    self.buffer = ""
                    matched = True
                    break
            if matched:
                break

            is_partial = any(prefix.startswith(self.buffer) for prefix in self.tool_prefixes)
            if is_partial and len(self.buffer) < self.max_prefix_len:
                break
            else:
                results.append((True, self.buffer[0]))
                self.buffer = self.buffer[1:]

        return results

    def finalize(self):
        results = []
        if self.buffer:
            results.append((not self.tool_diverted, self.buffer))
            self.buffer = ""
        return results
