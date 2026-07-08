// [AI 질문] 탭 — Astryx ai-chat 컴포넌트(@astryxdesign/core/Chat) 이식판.
// 인스펙터 컬럼(좁고 높이 고정)에 맞춰 풀페이지용 ChatLayout 셸 대신
// 경량 레이아웃을 쓴다: 스크롤은 메시지가 넘칠 때만, 컴포저는 하단 도크 고정.
import { useEffect, useRef } from "react";
import { ChatComposer, ChatMessage, ChatMessageBubble } from "@astryxdesign/core/Chat";
import { Avatar } from "@astryxdesign/core/Avatar";
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Markdown } from "@astryxdesign/core/Markdown";
import { Text } from "@astryxdesign/core/Text";
import { HStack, VStack } from "@astryxdesign/core/Layout";

type ChatEvidence = { title?: string; path?: string; snippet?: string };

export type AiChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence?: ChatEvidence[];
  refNodeIds?: string[];
};

export function AiChatPanel({
  keyReady,
  loading,
  messages,
  question,
  onQuestionChange,
  onSubmit,
  onHighlight,
}: {
  keyReady: boolean;
  loading: boolean;
  messages: AiChatMessage[];
  question: string;
  onQuestionChange: (value: string) => void;
  onSubmit: () => void;
  onHighlight?: (ids: string[] | null) => void;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const hasContent = messages.length > 0 || loading;

  // 새 메시지/로딩 시 하단으로 스크롤 (내용이 넘칠 때만 스크롤바가 생긴다)
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, loading]);

  return (
    <div className="ai-chat-astryx" data-astryx-theme="neutral" data-astryx-media="light">
      <div className="ai-chat-scroll" ref={scrollRef}>
        {!keyReady ? (
          <div className="ai-chat-keywarn">
            <Banner
              status="warning"
              title="OpenAI API key가 필요합니다"
              description="상단 OpenAI API 버튼에서 key를 등록하고 Test key로 검증하세요. (비용 발생)"
            />
          </div>
        ) : null}
        {hasContent ? (
          <div className="ai-chat-messages">
            {messages.map((message) =>
              message.role === "user" ? (
                <ChatMessage key={message.id} sender="user">
                  <ChatMessageBubble>{message.content}</ChatMessageBubble>
                </ChatMessage>
              ) : (
                // avatar를 ChatMessage에 넘기면 옆 컬럼을 항상 차지해 좁은 인스펙터 폭에서
                // 답변 앞에 큰 여백이 생긴다. 대신 ChatMessageBubble의 name 슬롯(버블 위 렌더)에
                // 아바타를 놓아 "A / 그 아래 답변" 형태로 쌓는다 — 전체 폭을 답변에 쓴다.
                <ChatMessage key={message.id} sender="assistant">
                  <ChatMessageBubble
                    variant="ghost"
                    name={
                      <span className="ai-chat-avatar-name">
                        <Avatar name="AI" size="small" />
                      </span>
                    }
                  >
                    <Markdown density="compact">{message.content}</Markdown>
                  </ChatMessageBubble>
                  {message.evidence?.length ? (
                    <VStack gap={1} className="ai-chat-evidence">
                      {message.evidence.slice(0, 3).map((item, index) => (
                        <Text key={`${message.id}-ev-${index}`} type="supporting" color="secondary">
                          📄 {item.title || item.path || `근거 ${index + 1}`}
                        </Text>
                      ))}
                    </VStack>
                  ) : null}
                  {message.refNodeIds?.length && onHighlight ? (
                    <HStack gap={2} className="ai-chat-highlight-row">
                      <Button
                        label={`그래프에서 보기 (${message.refNodeIds.length})`}
                        size="sm"
                        variant="secondary"
                        onClick={() => onHighlight(message.refNodeIds ?? null)}
                      />
                      <Button label="해제" size="sm" variant="ghost" onClick={() => onHighlight(null)} />
                    </HStack>
                  ) : null}
                </ChatMessage>
              ),
            )}
            {loading ? (
              <ChatMessage sender="assistant">
                <ChatMessageBubble
                  variant="ghost"
                  name={
                    <span className="ai-chat-avatar-name">
                      <Avatar name="AI" size="small" />
                    </span>
                  }
                >
                  답변을 생성중입니다…
                </ChatMessageBubble>
              </ChatMessage>
            ) : null}
          </div>
        ) : (
          <div className="ai-chat-empty">
            <Text type="label" weight="semibold">
              프로젝트 데이터에 바로 질문하세요
            </Text>
            <Text type="supporting" color="secondary">
              지식그래프를 근거로 답변합니다.
            </Text>
          </div>
        )}
      </div>

      <div className="ai-chat-dock">
        <ChatComposer
          density="compact"
          value={question}
          onChange={onQuestionChange}
          onSubmit={() => onSubmit()}
          placeholder={keyReady ? "그래프에 질문하기…" : "OpenAI API key를 먼저 등록·검증하세요"}
          isDisabled={!keyReady || loading}
        />
      </div>
    </div>
  );
}
