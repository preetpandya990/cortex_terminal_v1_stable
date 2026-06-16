"use client"

import * as React from "react"

interface TabsContextValue {
  value: string
  onValueChange: (value: string) => void
  /** Stable base ID used to generate aria-* linking attributes. */
  baseId: string
}

const TabsContext = React.createContext<TabsContextValue | undefined>(undefined)

interface TabsProps {
  value?: string
  defaultValue?: string
  onValueChange?: (value: string) => void
  children: React.ReactNode
  className?: string
}

const Tabs = ({ value: controlledValue, defaultValue, onValueChange, children, className }: TabsProps) => {
  const [internalValue, setInternalValue] = React.useState(defaultValue || "")
  const value = controlledValue !== undefined ? controlledValue : internalValue
  const baseId = React.useId()

  const handleValueChange = (newValue: string) => {
    if (controlledValue === undefined) {
      setInternalValue(newValue)
    }
    onValueChange?.(newValue)
  }

  return (
    <TabsContext.Provider value={{ value, onValueChange: handleValueChange, baseId }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  )
}

const TabsList = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, onKeyDown, ...props }, ref) => {
  const context = React.useContext(TabsContext)
  if (!context) throw new Error("TabsList must be used within Tabs")

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    // Allow parent handlers to run first.
    onKeyDown?.(e)

    const tabs = Array.from(
      e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    )
    if (tabs.length === 0) return

    const currentIndex = tabs.findIndex((t) => t === document.activeElement)

    let nextIndex: number | null = null

    switch (e.key) {
      case "ArrowRight":
        e.preventDefault()
        nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % tabs.length
        break
      case "ArrowLeft":
        e.preventDefault()
        nextIndex =
          currentIndex < 0
            ? tabs.length - 1
            : (currentIndex - 1 + tabs.length) % tabs.length
        break
      case "Home":
        e.preventDefault()
        nextIndex = 0
        break
      case "End":
        e.preventDefault()
        nextIndex = tabs.length - 1
        break
      default:
        return
    }

    if (nextIndex === null) return

    const target = tabs[nextIndex]
    const tabValue = target.dataset.tabValue
    if (tabValue) {
      context.onValueChange(tabValue)
      // Focus the tab after React re-renders it with tabIndex=0.
      requestAnimationFrame(() => target.focus())
    }
  }

  return (
    <div
      ref={ref}
      role="tablist"
      className={`inline-flex h-10 items-center justify-center rounded-md bg-gray-100 p-1 text-gray-500 ${className || ""}`}
      onKeyDown={handleKeyDown}
      {...props}
    />
  )
})
TabsList.displayName = "TabsList"

interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ className, value: triggerValue, ...props }, ref) => {
    const context = React.useContext(TabsContext)
    if (!context) throw new Error("TabsTrigger must be used within Tabs")

    const isActive = context.value === triggerValue

    return (
      <button
        ref={ref}
        type="button"
        role="tab"
        aria-selected={isActive}
        aria-controls={`${context.baseId}-panel-${triggerValue}`}
        id={`${context.baseId}-tab-${triggerValue}`}
        data-tab-value={triggerValue}
        tabIndex={isActive ? 0 : -1}
        className={`inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-white transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 ${
          isActive ? "bg-white text-gray-900 shadow-sm" : "text-gray-600 hover:text-gray-900"
        } ${className || ""}`}
        onClick={() => context.onValueChange(triggerValue)}
        {...props}
      />
    )
  }
)
TabsTrigger.displayName = "TabsTrigger"

interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
}

const TabsContent = React.forwardRef<HTMLDivElement, TabsContentProps>(
  ({ className, value: contentValue, ...props }, ref) => {
    const context = React.useContext(TabsContext)
    if (!context) throw new Error("TabsContent must be used within Tabs")

    if (context.value !== contentValue) return null

    return (
      <div
        ref={ref}
        role="tabpanel"
        tabIndex={0}
        id={`${context.baseId}-panel-${contentValue}`}
        aria-labelledby={`${context.baseId}-tab-${contentValue}`}
        className={`mt-2 ring-offset-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400 focus-visible:ring-offset-2 ${className || ""}`}
        {...props}
      />
    )
  }
)
TabsContent.displayName = "TabsContent"

export { Tabs, TabsList, TabsTrigger, TabsContent }
