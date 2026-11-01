import { useState } from "react";
import type { KeyboardEvent } from "react";

export interface HudDropdownProps {
  label: string;
  value: string;
  options: readonly string[];
  onChange: (v: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

/**
 * HUD 风格自绘下拉框。不用原生 select，是为了让选项、悬停态、字体
 * 完全受设计令牌控制；键盘交互（方向键/回车/Esc/Tab）全部监听在触发器上，
 * 焦点始终留在触发器，不进入列表项。
 */
export function HudDropdown({
  label,
  value,
  options,
  onChange,
  disabled = false,
  placeholder = "",
}: HudDropdownProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const selectedIndex = options.indexOf(value);

  // 展开时把活动项定位到当前选中项，键盘回车即可原地确认，不用重新导航
  const openList = (): void => {
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
    setOpen(true);
  };

  const closeList = (): void => setOpen(false);

  const pick = (index: number): void => {
    if (index >= 0 && index < options.length) {
      onChange(options[index]);
    }
    closeList();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>): void => {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        openList();
        return;
      }
      const total = options.length;
      if (total === 0) return;
      const delta = event.key === "ArrowDown" ? 1 : -1;
      // 取模实现首尾循环，让长列表到底后能直接绕回顶部
      setActiveIndex((prev) => (prev + delta + total) % total);
      return;
    }
    if (event.key === "Enter") {
      // 阻止触发器把回车再当一次点击；关闭态按 Enter 等价于点击展开（与 Space 行为一致）
      event.preventDefault();
      if (open) pick(activeIndex);
      else openList();
      return;
    }
    if (event.key === "Escape") {
      // 关闭态不拦截：Esc 要穿透给外层设置弹窗，否则弹窗在触发器聚焦时关不掉
      if (!open) return;
      event.stopPropagation();
      closeList();
      return;
    }
    if (event.key === "Tab") {
      // Tab 走正常焦点流，只顺手收起列表，避免残影盖住下一个控件
      closeList();
    }
  };

  const showPlaceholder = value.length === 0 && placeholder.length > 0;

  return (
    <div className="relative">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        disabled={disabled}
        onClick={() => (open ? closeList() : openList())}
        onKeyDown={onKeyDown}
        className={`flex w-full min-w-0 items-center justify-between gap-2 rounded-[2px] border border-edge px-2 py-2 text-left text-sm text-ink ${
          disabled ? "cursor-not-allowed bg-row text-ink-muted" : "bg-canvas"
        }`}
      >
        <span
          className={`min-w-0 truncate font-mono text-xs ${
            disabled || showPlaceholder ? "text-ink-muted" : "text-ink"
          }`}
        >
          {value || placeholder || "\u00a0"}
        </span>
        <svg
          aria-hidden="true"
          viewBox="0 0 14 14"
          width={14}
          height={14}
          className={`shrink-0 text-ink-muted ${open ? "rotate-180" : ""}`}
        >
          <path d="M2 4 L6 8 L10 4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>

      {open && (
        <>
          {/* 透明接缝层：铺满全屏承接"点击外部关闭"，不加任何白底或遮罩，视觉上无痕。
              用 div 而不是 button：它不是控件（键盘用户走 Esc/Tab 收起），只是接住外点。 */}
          <div
            role="presentation"
            aria-hidden="true"
            onClick={closeList}
            className="fixed inset-0 z-40 cursor-default border-0 bg-transparent p-0"
          />
          <ul
            role="listbox"
            aria-label={label}
            className="absolute z-50 mt-1 max-h-60 w-full min-w-0 overflow-y-auto border border-edge bg-panel shadow-lg"
          >
            {options.map((option, index) => {
              const isSelected = option === value;
              const isActive = index === activeIndex;
              return (
                <li
                  key={option}
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => pick(index)}
                  onMouseEnter={() => setActiveIndex(index)}
                  className={`cursor-pointer px-3 py-2 font-mono text-xs ${
                    isSelected ? "bg-accent text-accent-ink" : isActive ? "bg-row text-ink" : "text-ink"
                  }`}
                >
                  {option}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}
