// [AI 질문] 탭 — Astryx ai-chat 템플릿(@astryxdesign/cli templates/pages/ai-chat) 이식판.
// 그래프 인스펙터 컬럼 폭에 맞춰 아티팩트 분할 패널은 제외하고
// ChatLayout + ChatMessageList + ChatComposer 구성을 사용한다.
import {
  ChatComposer,
  ChatLayout,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageList,
} from "@astryxdesign/core/Chat";
import { Avatar } from "@astryxdesign/core/Avatar";
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
  return (
    <div className="ai-chat-astryx" data-astryx-theme="neutral" data-astryx-media="light">
      <ChatLayout
        density="compact"
        emptyState={
          <VStack gap={2} hAlign="center">
            <Text type="label" weight="semibold">
              프로젝트 데이터에 바로 질문하세요
            </Text>
            <Text type="supporting" color="secondary">
              선택한 온톨로지 팩의 문서, 노드, 관계를 근거로 답변합니다.
            </Text>
          </VStack>
        }
        composer={
          <ChatComposer
            density="compact"
            value={question}
            onChange={onQuestionChange}
            onSubmit={() => onSubmit()}
            placeholder={keyReady ? "그래프에 질문하기…" : "OpenAI API key를 먼저 등록·검증하세요"}
            isDisabled={!keyReady || loading}
            status={
              keyReady
                ? undefined
                : { type: "warning", message: "상단 OpenAI API 버튼에서 key를 등록하고 Test key로 검증하세요. (비용 발생)" }
            }
          />
        }
      >
        {messages.length === 0 && !loading ? null : (
        <ChatMessageList>
          {messages.map((message) =>
            message.role === "user" ? (
              <ChatMessage key={message.id} sender="user">
                <ChatMessageBubble>{message.content}</ChatMessageBubble>
              </ChatMessage>
            ) : (
              <ChatMessage key={message.id} sender="assistant" avatar={<Avatar name="AI" size="small" />}>
                <ChatMessageBubble variant="ghost">
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
            <ChatMessage sender="assistant" avatar={<Avatar name="AI" size="small" />}>
              <ChatMessageBubble variant="ghost">답변을 생성중입니다…</ChatMessageBubble>
            </ChatMessage>
          ) : null}
        </ChatMessageList>
        )}
      </ChatLayout>
    </div>
  );
}
