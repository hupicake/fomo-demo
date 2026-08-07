export type ChatMessageSender = (message: { text: string }) => Promise<void>;

/**
 * Clears a recoverable chat error before forwarding the captured prompt. The
 * caller keeps ownership of input clearing, so the message is never discarded
 * before the next send is attempted.
 */
export async function submitChatMessage({
  clearError,
  sendMessage,
  text,
}: {
  clearError: () => void;
  sendMessage: ChatMessageSender;
  text: string;
}): Promise<boolean> {
  const content = text.trim();
  if (!content) return false;

  clearError();
  await sendMessage({ text: content });
  return true;
}
