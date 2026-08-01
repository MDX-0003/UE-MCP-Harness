# 10 — TypeScript 语法速览（面向 C++/Python 程序员）

**目标读者**：熟练使用 C++ 和 Python，但对 TypeScript 语法不熟悉的开发者
**覆盖范围**：仅涉及 PiAgent 文档中出现过的语法，不涉及未在文档中出现的语言特性

**阅读建议**：遇到看不懂的 TS 代码时回来查对应的章节，不需要一次性读完。

---

## 1. 变量声明：`const`、`let`、`readonly`

```typescript
// C++: const int x = 42;
// Python: x = 42（常量化靠惯例）
const x = 42;           // 值不可重新赋值（但对象内部可修改）
x = 43;                 // ❌ 编译错误

// C++: int y = 0; y = 1;
// Python: y = 0; y = 1
let y = 0;              // 可重新赋值
y = 1;                  // ✅

// 对象/数组声明为 const 时，引用不变但内容可改
const arr = [1, 2, 3];
arr.push(4);            // ✅ — 数组内容可变
arr = [5, 6];           // ❌ — 但不能重新绑定 arr 这个名字

// C++: const std::string& name — 只读引用
// TypeScript 中 readonly 用于类和接口的属性
interface AgentState {
  readonly isStreaming: boolean;  // 只能在构造时赋值，之后不可改
  systemPrompt: string;           // 随时可改
}
```

---

## 2. 类型标注：方向与 C++ 相反

```typescript
// C++:    Type name = value;
// TS:     name: Type = value;

// C++:  int count = 0;
// TS:
let count: number = 0;

// C++:  std::string name = "hello";
// TS:
let name: string = "hello";

// C++:  bool done = false;
// TS:
let done: boolean = false;

// C++:  void process(int x);
// TS:
function process(x: number): void { ... }

// TypeScript 有类型推断——很多时候不需要显式写类型
let count = 0;       // TS 推断为 number
let name = "hello";  // TS 推断为 string
const items = [1, 2, 3];  // TS 推断为 number[]
```

---

## 3. 基本类型

```typescript
// number — 不分 int/float/double，统一为 64 位浮点
let count: number = 42;
let price: number = 3.14;

// string — 支持单引号、双引号、模板字符串
let msg: string = "hello";
let msg2: string = 'world';
let msg3: string = `hello ${name}`;  // 模板字符串（见第 9 节）

// boolean — true / false
let done: boolean = true;

// void — 函数无返回值（类似 C++ void / Python None）
// undefined — 值未定义（类似 Python 的 None）
function log(x: string): void { console.log(x); }
let x: string | undefined = undefined;  // 可能是字符串，可能是 undefined

// null — 显式空值（在 PiAgent 中很少出现，主流用 undefined）
let y: string | null = null;  // 不常用

// any — 关闭类型检查（尽量不用）
let whatever: any = 42;
whatever = "now a string";  // ✅ 但失去了类型安全

// unknown — 安全的 any（必须先检查类型才能使用）
let unknown: unknown = 42;
// unknown.toFixed(2);  // ❌ 编译错误——必须先用 typeof 检查
if (typeof unknown === "number") {
  unknown.toFixed(2);    // ✅
}
```

---

## 4. 数组和泛型

```typescript
// C++:  std::vector<int> nums;
// Python: nums: list[int]
// TS:
let nums: number[] = [1, 2, 3];
let strs: string[] = ["a", "b"];

// 泛型写法（等效）
let nums2: Array<number> = [1, 2, 3];

// C++:  std::set<std::string>
// TS:
let idSet: Set<string> = new Set(["a", "b"]);

// C++:  std::map<std::string, int>
// TS:
let nameMap: Map<string, number> = new Map([["a", 1], ["b", 2]]);

// 泛型的含义：<T> 是类型参数，就像 C++ 的 template<typename T>
// AgentTool<T> — "AgentTool 接受一个类型参数 T"
// ReadonlySet<string> — "只读集合，元素是 string"
// Promise<void> — "返回 void 的 Promise"（见第 8 节）
```

---

## 5. 接口（Interface）和类型别名（Type）

```typescript
// 接口：描述对象的结构（类似 C++ struct，但没有方法实现）
// C++:
//   struct User {
//     std::string name;
//     int age;
//   };

interface User {
  name: string;
  age: number;
}

const user: User = { name: "Alice", age: 30 };

// 可选属性（?）
interface Options {
  required: string;      // 必须有
  optional?: string;     // 可以有、可以没有
}

const opt1: Options = { required: "x" };              // ✅
const opt2: Options = { required: "x", optional: "y" }; // ✅

// 只读属性（readonly）
interface Config {
  readonly port: number;  // 构造后不可改
  host: string;           // 可改
}

// 类型别名：给类型起个别名，或定义联合类型
type ID = string;                          // ID 就是 string
type Status = "idle" | "running" | "done"; // 只能是这三个字符串之一
type MaybeNumber = number | undefined;     // number 或 undefined

// interface vs type 的区别（粗略版）：
// - interface 主要用于描述对象结构，可以被"合并"（declaration merging）
// - type 更灵活，可以表示联合类型、交叉类型等
// - PiAgent 中两者混用，不用纠结选哪个
```

---

## 6. 联合类型和判别联合

```typescript
// 联合类型：A | B — "A 或者 B"
// C++:  std::variant<int, std::string>
// Python: int | str
let value: string | number = "hello";
value = 42;  // ✅

// ── 判别联合（Discriminated Union）──
// PiAgent 中最常见的模式——事件系统全靠这个

// Python 等价概念：
//   match event:
//       case {"type": "message_start", "message": msg}: ...
//       case {"type": "message_end", "message": msg}: ...

type AgentEvent =
  | { type: "agent_start" }                                          // 只有 type
  | { type: "agent_end"; messages: AgentMessage[] }                  // type + messages
  | { type: "message_start"; message: AgentMessage }                 // type + message
  | { type: "message_update"; message: AgentMessage; delta: Token }; // type + 两个字段
  // ... 更多变体 ...

// 使用方式：每个变体通过 type 字段区分
function handleEvent(event: AgentEvent) {
  switch (event.type) {
    case "agent_start":
      // 这里 event 被缩窄为 { type: "agent_start" }
      // 尝试访问 event.messages → ❌ 编译错误
      console.log("Agent started");
      break;

    case "agent_end":
      // 这里 event 被缩窄为 { type: "agent_end"; messages: AgentMessage[] }
      console.log(`Agent ended with ${event.messages.length} messages`);
      break;

    case "message_start":
      // event 被缩窄为 { type: "message_start"; message: AgentMessage }
      console.log(event.message);
      break;
  }
}

// 这是 PiAgent 整个事件系统的基础 ——
// ExtensionEvent、AgentEvent、AgentSessionEvent 全部是这个模式
```

---

## 7. 箭头函数

```typescript
// 箭头函数：(参数) => 返回值类型
// Python: lambda x: x + 1
// C++:   [](int x) { return x + 1; }

// 完整形式
const add: (a: number, b: number) => number = (a, b) => {
  return a + b;
};

// 简写形式（单表达式、无花括号）
const add2 = (a: number, b: number) => a + b;

// 单参数可省略括号
const double = (x: number) => x * 2;

// 无参数
const greet = () => "hello";

// ── 在 PiAgent 中的实际使用 ──

// 作为回调函数传入
agent.subscribe(async (event, signal) => {
  switch (event.type) {
    case "message_end":
      // 处理消息结束
      break;
  }
});

// 等价于 Python：
// async def handler(event, signal):
//     match event.type:
//         case "message_end":
//             pass
//
// agent.subscribe(handler)
```

---

## 8. Async/Await 和 Promise

```typescript
// TypeScript 的 async/await 语义与 Python 几乎完全一致

// Python:
//   async def fetch_data() -> str:
//       result = await http_client.get(url)
//       return result

// TypeScript:
async function fetchData(): Promise<string> {
  const result = await httpClient.get(url);
  return result;
}

// ⚠ 关键区别：返回类型标注为 Promise<T>，不是 T
// Python:  async def foo() -> str:      ← 标注返回值类型为 str
// TS:      async function foo(): Promise<string> ← 标注为 Promise<string>

// Promise<T> 类似 Python 的 Coroutine[T, Any, T]
// Promise<string> = "我将来会给你一个 string"

// ── 常见模式 ──
// 调用 async 函数必须 await（在 async 上下文中）
const data = await fetchData();  // data 的类型是 string（自动解包）

// 如果不 await，得到的是 Promise 对象
const promise = fetchData();     // promise 的类型是 Promise<string>

// 并行执行多个 async 调用（类似 Python asyncio.gather）
const [r1, r2] = await Promise.all([
  fetchA(),
  fetchB(),
]);

// 在箭头函数中使用 async：
const handler = async (event: AgentEvent) => {
  const result = await someAsyncOp();
  console.log(result);
};
```

---

## 9. 模板字符串

```typescript
// Python: f"hello {name}, you have {count} messages"
// C++:    std::format("hello {}, you have {} messages", name, count)
// TS:     `hello ${name}, you have ${count} messages`

const name = "Alice";
const count = 5;
const msg = `hello ${name}, you have ${count} messages`;
// → "hello Alice, you have 5 messages"

// 支持多行（不需要 \n 或 + 连接）
const multiline = `
第一行
第二行
第三行 ${name} 结束
`;
```

---

## 10. 解构和展开

```typescript
// ── 对象解构 ──
// Python: name, age = user["name"], user["age"]
const user = { name: "Alice", age: 30, city: "NYC" };
const { name, age } = user;
// name = "Alice", age = 30

// 可以在函数参数中直接解构
function greet({ name, age }: { name: string; age: number }) {
  console.log(`${name} is ${age}`);
}
greet(user);  // Alice is 30

// ── 数组解构 ──
// Python: a, b, c = [1, 2, 3]
const [a, b, c] = [1, 2, 3];  // a=1, b=2, c=3

// ── 展开运算符 ──
// 对象展开（类似 Python **dict 合并）
const defaults = { host: "localhost", port: 8000 };
const custom = { ...defaults, port: 9000 };  // { host: "localhost", port: 9000 }
// 后面覆盖前面

// 数组展开（类似 Python *list）
const arr1 = [1, 2];
const arr2 = [0, ...arr1, 3];  // [0, 1, 2, 3]
```

---

## 11. import / export

```typescript
// ── 命名导出/导入 ──
// 导出方
export function greet(name: string): string { return `hello ${name}`; }
export const VERSION = "1.0";
export interface Config { port: number; }

// 导入方
import { greet, VERSION, Config } from "./utils";
// 或者导入全部
import * as Utils from "./utils";
Utils.greet("Alice");

// ── 类型导入（编译后会被擦除，不影响运行时）──
import type { AgentEvent, AgentMessage } from "@earendil-works/pi-agent-core";
//      ^^^^ 告诉 TS：这只用于类型检查，编译成 JS 后删除

// ── 默认导出/导入 ──
// 导出方
export default function main() { ... }

// 导入方（不需要花括号，名字可以任意）
import myMain from "./main";
```

---

## 12. 可选链、空值合并

```typescript
// ── 可选链 ?. ──
// Python: user.get("address", {}).get("city")  — 用 dict.get 避免 KeyError
// C++:    user && user->address ? user->address->city : nullptr  — 手写判空

const city = user?.address?.city;
// 等价于：
//   if (user === null || user === undefined) return undefined;
//   if (user.address === null || user.address === undefined) return undefined;
//   return user.address.city;

// 常用于安全检查可能为 undefined 的属性
const result = hookResult?.content;  // 如果 hookResult 是 undefined，result 也是 undefined

// ── 空值合并 ?? ──
// Python: value = x if x is not None else default
const port = config.port ?? 8000;  // 如果 config.port 是 null 或 undefined，用 8000

// ⚠ ?? 只检查 null 和 undefined，不检查 0 或 ""
const count = 0 ?? 42;     // → 0（0 不是 null/undefined）
const name = "" ?? "默认";  // → ""（空字符串不是 null/undefined）

// ── 非空断言 ! ──
// 告诉 TS："我确定这个值不是 null/undefined，不需要你检查"
// 类似 C++ 的 static_cast — 绕过类型检查
const value = maybeUndefined!;  // 危险：如果实际是 undefined，运行时崩溃
```

---

## 13. 常见惯用法

```typescript
// ── 方法简写 ──
// 传统写法
const obj = {
  name: "Alice",
  greet: function() { return `hello`; },
};
// 简写
const obj2 = {
  name: "Alice",
  greet() { return `hello`; },  // 省略 function 关键字
};

// ── 属性简写 ──
const name = "Alice";
const age = 30;
const user = { name, age };  // 等价于 { name: name, age: age }

// ── 回调模式 ──
// PiAgent 大量使用这种"传入函数作为参数"的模式
agent.subscribe((event) => { ... });              // 订阅
pi.on("tool_call", async (event) => { ... });     // 事件
pi.registerTool({ ..., execute: async (params) => { ... } });  // 工具定义

// ── 条件返回 undefined ──
function maybeGetConfig(): Config | undefined {
  if (configExists) {
    return { port: 8000 };
  }
  // 不写 return — 隐式返回 undefined（类似 Python 无 return 时返回 None）
}
```

---

## 14. TypeBox 速览（工具参数 Schema）

```typescript
import { Type } from "typebox";  // 类似 Python 的 pydantic / jsonschema

// ── 为什么需要 TypeBox ──
// PiAgent 的工具需要"运行时校验 LLM 传的参数是否合法"
// TypeBox 做的事情：定义 schema → 运行时校验 + 生成 TypeScript 类型

// ── 基础类型 ──
Type.String()    // → string
Type.Number()    // → number
Type.Boolean()   // → boolean

// ── 对象 ──
const PingSchema = Type.Object({
  // 空对象 = 无参数
});

const ReadSchema = Type.Object({
  filePath: Type.String(),            // 必需字段
  offset: Type.Optional(Type.Number()),  // 可选字段
  limit: Type.Optional(Type.Number()),
});
// 等价于 Pydantic:
// class ReadSchema(BaseModel):
//     file_path: str
//     offset: int | None = None
//     limit: int | None = None

// ── 数组、联合 ──
Type.Array(Type.String())             // string[]
Type.Union([Type.String(), Type.Number()])  // string | number

// ── 静态类型推导 ──
// Static<T> — 从 TypeBox schema 推导出对应的 TypeScript 类型
import type { Static } from "typebox";
type ReadParams = Static<typeof ReadSchema>;
// ReadParams = { filePath: string; offset?: number; limit?: number }

// ── 在工具注册中使用 ──
pi.registerTool({
  name: "read",
  description: "Read a file",
  parameters: Type.Object({
    filePath: Type.String(),
    offset: Type.Optional(Type.Number()),
  }),
  //         ↓ params 自动获得类型 { filePath: string; offset?: number }
  async execute(toolCallId, params, signal, onUpdate, ctx) {
    console.log(params.filePath);  // ✅ params.filePath 的类型是 string
    // console.log(params.nonexistent);  // ❌ 编译错误
  },
});
```

---

## 15. 速查表：C++ / Python → TypeScript

| 概念 | C++ | Python | TypeScript |
|------|-----|--------|-----------|
| 声明常量 | `const int x = 1;` | `x = 1` (惯例) | `const x = 1;`（值不可重新赋值） |
| 声明变量 | `int x = 1;` | `x = 1` | `let x = 1;` |
| 类型标注 | `int x;` | `x: int` | `x: number;` |
| 函数 | `int f(int x)` | `def f(x: int) -> int:` | `function f(x: number): number` |
| Lambda/闭包 | `[](int x){ return x+1; }` | `lambda x: x+1` | `(x: number) => x + 1` |
| 枚举/variant | `std::variant<A,B>` | `A \| B` (3.10+) | `A \| B` |
| 空值 | `nullptr` / `std::nullopt` | `None` | `undefined`（主流）/ `null`（罕见） |
| 字符串插值 | `std::format("hi {}", name)` | `f"hi {name}"` | `` `hi ${name}` `` |
| 字典/Map | `std::map<string, string>` | `dict[str, str]` | `Map<string, string>` |
| 对象/结构体 | `struct { int x; }` | `{"x": 1}` | `{ x: 1 }` 或 `interface` |
| 数组 | `std::vector<int>` | `list[int]` | `number[]` |
| 异步函数 | `std::future<T>` | `async def f() -> T:` | `async function f(): Promise<T>` |
| 模块导入 | `#include "x.h"` | `import x` | `import { x } from "./x"` |
| 可选属性 | — | `dict.get("key")` 惯用法 | `obj?.prop`（可选链）|
| 运行时 schema | — | `pydantic` | `TypeBox` |

---

## 16. 阅读 PiAgent 文档时的常见困惑点

### Q: `Promise<void>` 是什么？

`void` 函数的 async 版本。`void` = "不返回有意义的值"，`Promise<void>` = "一个将来完成的异步操作，完成后不返回值"。

```typescript
async function doSomething(): Promise<void> {
  await someIO();
  // 不 return — 隐式 return undefined
}
```

### Q: `string | undefined` 为什么到处都是？

TypeScript 强制处理"可能不存在"的情况。`string | undefined` = "可能是 string，也可能是 undefined（还没赋值/不存在）"。这比 Python 的隐式 None 更安全——编译器会强迫你处理 undefined 的情况。

### Q: `{ type: "hello"; version: number; token: string }` 这种写法是什么？

这是**内联类型字面量**（inline type literal）——没有给它起名字，直接在参数/返回值位置写对象结构。PiAgent 的协议 schemas 大量使用这种写法。等价于：

```typescript
// 内联写法
function connect(hello: { type: "hello"; version: number; token: string }) { ... }

// 等价于：
interface ClientHello {
  type: "hello";
  version: number;
  token: string;
}
function connect(hello: ClientHello) { ... }
```

### Q: `await` 只能在 `async` 函数里用吗？

是的。和 Python 完全一样——`await` 只能出现在 `async function` 内部。箭头函数也可以是 `async`：`async () => { await ... }`。

### Q: `import type` 和 `import` 有什么区别？

`import type` — 只在编译时存在，编译为 JS 后被完全删除。用于导入接口/类型定义。
`import` — 同时导入值和类型，编译后保留。

如果只用到类型，用 `import type` 可以避免运行时的依赖（以及循环依赖问题）。
