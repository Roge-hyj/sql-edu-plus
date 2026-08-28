<script setup lang="ts">
import { onLaunch, onShow, onHide } from "@dcloudio/uni-app";
onLaunch(() => {
  console.log("App Launch");
});
onShow(() => {
  console.log("App Show");
});
onHide(() => {
  console.log("App Hide");
});
</script>

<style lang="scss">
/* ================= 设计系统全局样式 ================= */

page {
  background: $bg-page;
  color: $text-1;
  font-size: 28rpx;
  line-height: 1.6;
  font-family:
    -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  -webkit-font-smoothing: antialiased;
}

/* 页面容器：移动端全宽，桌面端居中栏 */
.page {
  min-height: 100vh;
  padding: 24rpx 28rpx calc(48rpx + env(safe-area-inset-bottom));
  box-sizing: border-box;
}

/* ---------- 卡片 ---------- */
.card {
  background: $bg-card;
  border-radius: $radius-card;
  box-shadow: $shadow-card;
  padding: 30rpx;
  margin-bottom: 24rpx;
  box-sizing: border-box;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 14rpx;
  font-size: 30rpx;
  font-weight: 600;
  color: $text-1;
  margin-bottom: 20rpx;

  .dot {
    width: 10rpx;
    height: 10rpx;
    border-radius: 50%;
    background: $brand;
    flex-shrink: 0;
  }

  .extra {
    margin-left: auto;
    font-size: 24rpx;
    font-weight: 400;
    color: $text-3;
  }
}

/* ---------- 按钮 ---------- */
.btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10rpx;
  border-radius: $radius-btn;
  font-size: 30rpx;
  font-weight: 600;
  padding: 22rpx 32rpx;
  border: none;
  transition: opacity 0.15s ease, transform 0.05s ease;

  &::after {
    border: none;
  }

  &[disabled],
  &.is-disabled {
    opacity: $uni-opacity-disabled;
  }

  &:active:not([disabled]) {
    transform: translateY(2rpx);
  }
}

.btn-primary {
  background: $brand-gradient;
  color: #fff;
  box-shadow: 0 12rpx 28rpx rgba(76, 111, 255, 0.28);
}

.btn-ghost {
  background: $brand-soft;
  color: $brand-deep;
}

.btn-plain {
  background: #fff;
  color: $text-2;
  border: 2rpx solid $border-color;
}

.btn-danger {
  background: $danger-soft;
  color: $danger;
}

.btn-sm {
  font-size: 24rpx;
  padding: 12rpx 24rpx;
  border-radius: 12rpx;
  font-weight: 500;
}

.btn-block {
  width: 100%;
}

/* ---------- 表单 ---------- */
.field {
  margin-bottom: 24rpx;
}

.field-label {
  display: block;
  font-size: 26rpx;
  font-weight: 500;
  color: $text-2;
  margin-bottom: 12rpx;
}

.input,
.textarea {
  width: 100%;
  box-sizing: border-box;
  background: #f7f9fd;
  border: 2rpx solid transparent;
  border-radius: 16rpx;
  padding: 20rpx 24rpx;
  font-size: 28rpx;
  color: $text-1;
  transition: border-color 0.15s ease, background 0.15s ease;

  &::placeholder {
    color: $uni-text-color-placeholder;
  }

  &:focus {
    border-color: $brand;
    background: #fff;
  }
}

.textarea {
  min-height: 120rpx;
  line-height: 1.55;
}

.input-mono {
  font-family: $font-mono;
  font-size: 26rpx;
}

/* ---------- 徽章 / 标签 ---------- */
.chip {
  display: inline-flex;
  align-items: center;
  gap: 8rpx;
  padding: 6rpx 18rpx;
  border-radius: 999rpx;
  font-size: 22rpx;
  font-weight: 500;
  line-height: 1.5;
  background: $bg-page;
  color: $text-2;
  white-space: nowrap;
}

.chip-brand {
  background: $brand-soft;
  color: $brand-deep;
}

.chip-success {
  background: $success-soft;
  color: $success;
}

.chip-warning {
  background: $warning-soft;
  color: #C77D00;
}

.chip-danger {
  background: $danger-soft;
  color: $danger;
}

/* ---------- 文本工具 ---------- */
.text-secondary {
  color: $text-2;
}

.text-muted {
  color: $text-3;
}

.text-danger {
  color: $danger;
}

.section-gap {
  height: 24rpx;
}

/* ---------- Markdown 渲染（rich-text 节点类） ---------- */
.md-body {
  font-size: 27rpx;
  line-height: 1.75;
  color: $text-1;
  word-break: break-word;
}

.md-p {
  margin: 6rpx 0;
}

.md-h3,
.md-h4,
.md-h5 {
  font-weight: 600;
  margin: 18rpx 0 8rpx;
  color: $text-1;
}

.md-h3 {
  font-size: 30rpx;
}

.md-h4 {
  font-size: 28rpx;
}

.md-h5 {
  font-size: 27rpx;
}

.md-ul {
  margin: 8rpx 0;
  padding-left: 34rpx;
}

.md-ul li {
  margin: 6rpx 0;
}

.md-code {
  font-family: $font-mono;
  font-size: 25rpx;
  background: #eef1f8;
  color: #44506b;
  padding: 2rpx 12rpx;
  border-radius: 8rpx;
}

.md-pre {
  font-family: $font-mono;
  font-size: 25rpx;
  line-height: 1.6;
  background: #101528;
  color: #d6e2ff;
  border-radius: 16rpx;
  padding: 22rpx 26rpx;
  margin: 14rpx 0;
  overflow-x: auto;
  white-space: pre;
}

/* ---------- 动画 ---------- */
@keyframes fade-slide-up {
  from {
    opacity: 0;
    transform: translateY(16rpx);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.anim-in {
  animation: fade-slide-up 0.28s ease both;
}

@keyframes pulse-soft {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
}

.anim-pulse {
  animation: pulse-soft 1.4s ease-in-out infinite;
}
</style>
