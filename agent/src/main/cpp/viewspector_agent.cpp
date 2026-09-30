/*
 * ViewSpector — Native JVMTI bootstrap agent (libviewspector.so).
 *
 * This is layer (1) of the three on-device layers described in CONTRACT.md §2.
 * It is loaded into the inspected app process by:
 *
 *     adb shell cmd activity attach-agent <pkg> \
 *         /path/libviewspector.so=<bootstrapDexPath>:<payloadPath>:<socketName>
 *
 * The `options` string handed to Agent_OnAttach/Agent_OnLoad is everything
 * after the '=' sign, i.e. the colon-separated triple:
 *
 *     bootstrapDexPath:payloadPath:socketName
 *
 *   - bootstrapDexPath : absolute path to bootstrap.dex (added to the bootstrap
 *                        class loader search so FindClass can resolve
 *                        com/oberkfell/viewspector/agent/Bootstrap).
 *   - payloadPath      : absolute path to payload.jar (a dex-in-jar that the
 *                        Bootstrap layer loads with a DexClassLoader whose
 *                        parent is the *app* class loader).
 *   - socketName       : abstract LocalServerSocket name, e.g. viewspector_<pid>.
 *
 * The host MUST format the attach-agent options exactly as
 *   "<bootstrapDexPath>:<payloadPath>:<socketName>"
 * (see notes returned with this module). None of the three path/name tokens may
 * contain a ':' — absolute /data/... paths and viewspector_<pid> never do.
 *
 * Responsibilities (CONTRACT.md §2, modeled on
 *   tools-base/ui-inspector/agent/native/agent.cc and
 *   tools-base/app-inspection/native/src/app_inspection_service.cc):
 *   1. GetEnv a stand-alone JVMTI 1.2 environment. No capabilities are requested:
 *      nothing below needs one, and potential capabilities such as
 *      can_retransform_classes / can_access_local_variables switch ART into a
 *      slower, deoptimized mode for the whole app. (Only if silencing hidden-API
 *      enforcement fails without them are they added for a retry.)
 *   2. (Best effort) silence hidden-API enforcement so the payload may reach
 *      framework internals (WindowInspector, getUniqueDrawingId, Inspection
 *      companions, ...). Same mechanism as HiddenApiSilencer in tools-base.
 *   3. AddToBootstrapClassLoaderSearch(bootstrapDexPath), unless an earlier
 *      attach already made Bootstrap loadable (re-injection must not append the
 *      same dex to the boot class path again).
 *   4. DisposeEnvironment: both effects above are process-wide and outlive the
 *      env, which (with any capabilities it held) is released at once.
 *   5. FindClass com/oberkfell/viewspector/agent/Bootstrap, get its static
 *      initialize(String payloadPath, String socketName)V and call it.
 *
 * Only the attach path is supported: Agent_OnLoad (loading at VM start with
 * -agentpath) runs before the VM and the app exist, so it refuses with JNI_ERR.
 *
 * Self-contained on purpose: only <jni.h>, <jvmti.h>, <android/log.h>, <string>.
 */

#include <android/log.h>
#include <jni.h>
#include <jvmti.h>

#include <cstring>
#include <string>

namespace {

// ----------------------------------------------------------------- constants
constexpr const char* kLogTag = "ViewSpector";

// Layer (2) entrypoint that this native agent jumps to. See CONTRACT.md §3.
constexpr const char* kBootstrapClassName =
    "com/oberkfell/viewspector/agent/Bootstrap";
constexpr const char* kInitializeMethodName = "initialize";
// initialize(String payloadPath, String socketName) : void
constexpr const char* kInitializeMethodSignature =
    "(Ljava/lang/String;Ljava/lang/String;)V";

// ----------------------------------------------------------------- logging
// Thin wrappers so every step is traceable in logcat under tag "ViewSpector".
#define VS_LOGI(...) __android_log_print(ANDROID_LOG_INFO, kLogTag, __VA_ARGS__)
#define VS_LOGW(...) __android_log_print(ANDROID_LOG_WARN, kLogTag, __VA_ARGS__)
#define VS_LOGE(...) __android_log_print(ANDROID_LOG_ERROR, kLogTag, __VA_ARGS__)

// ----------------------------------------------------------------- options
// Parsed form of the colon-separated attach-agent options.
struct AgentOptions {
  std::string bootstrap_dex_path;
  std::string payload_path;
  std::string socket_name;
};

// Parses "bootstrapDexPath:payloadPath:socketName". Splits on the first two
// ':' only, so the trailing socket name token is taken verbatim. Returns false
// if any of the three tokens is empty or a ':' is missing.
bool ParseOptions(const char* options, AgentOptions* out) {
  if (options == nullptr || std::strlen(options) == 0) {
    VS_LOGE("Agent options are null/empty; expected '%s'",
            "bootstrapDexPath:payloadPath:socketName");
    return false;
  }

  std::string s(options);
  const std::string::size_type first = s.find(':');
  if (first == std::string::npos) {
    VS_LOGE("Agent options missing first ':' separator: '%s'", options);
    return false;
  }
  const std::string::size_type second = s.find(':', first + 1);
  if (second == std::string::npos) {
    VS_LOGE("Agent options missing second ':' separator: '%s'", options);
    return false;
  }

  out->bootstrap_dex_path = s.substr(0, first);
  out->payload_path = s.substr(first + 1, second - (first + 1));
  out->socket_name = s.substr(second + 1);

  if (out->bootstrap_dex_path.empty() || out->payload_path.empty() ||
      out->socket_name.empty()) {
    VS_LOGE(
        "Agent options have an empty token (bootstrap='%s' payload='%s' "
        "socket='%s')",
        out->bootstrap_dex_path.c_str(), out->payload_path.c_str(),
        out->socket_name.c_str());
    return false;
  }
  return true;
}

// ----------------------------------------------------------------- jvmti env
// Create a stand-alone jvmtiEnv to avoid callback conflicts with any other
// agent already attached. Mirrors profiler::CreateJvmtiEnv in
// tools-base/transport/native/jvmti/jvmti_helper.cc:28.
jvmtiEnv* CreateJvmtiEnv(JavaVM* vm) {
  jvmtiEnv* jvmti = nullptr;
  const jint result = vm->GetEnv(reinterpret_cast<void**>(&jvmti),
                                 JVMTI_VERSION_1_2);
  if (result != JNI_OK || jvmti == nullptr) {
    VS_LOGE("GetEnv(JVMTI_VERSION_1_2) failed: %d", result);
    return nullptr;
  }
  return jvmti;
}

// How bad a JVMTI error is for the install: kFatal ones abort it (logged at
// E), kRecoverable ones are worked around (logged at W). The host reads the
// agent's E lines while it waits for the socket and fails the attach on a
// fatal one, so a recoverable error must never be logged at E.
enum class JvmtiSeverity { kFatal, kRecoverable };

// Logs and returns true when err is not JVMTI_ERROR_NONE. Mirrors
// profiler::CheckJvmtiError in jvmti_helper.cc:47.
bool CheckJvmtiError(jvmtiEnv* jvmti, jvmtiError err, const char* what,
                     JvmtiSeverity severity) {
  if (err == JVMTI_ERROR_NONE) {
    return false;
  }
  char* name = nullptr;
  jvmti->GetErrorName(err, &name);
  const int priority = severity == JvmtiSeverity::kFatal ? ANDROID_LOG_ERROR
                                                          : ANDROID_LOG_WARN;
  __android_log_print(priority, kLogTag, "JVMTI error %d(%s) during %s", err,
                      name == nullptr ? "Unknown" : name, what);
  if (name != nullptr) {
    jvmti->Deallocate(reinterpret_cast<unsigned char*>(name));
  }
  return true;
}

// Adds every potential capability. Mirrors profiler::SetAllCapabilities in
// jvmti_helper.cc:61. Used ONLY as a fallback when silencing hidden-API
// enforcement fails without capabilities: several potential capabilities make
// ART deoptimize the whole app, so the default path requests none. The env is
// disposed right after install, which relinquishes them again.
bool AddPotentialCapabilities(jvmtiEnv* jvmti) {
  jvmtiCapabilities caps;
  std::memset(&caps, 0, sizeof(caps));
  if (CheckJvmtiError(jvmti, jvmti->GetPotentialCapabilities(&caps),
                      "GetPotentialCapabilities", JvmtiSeverity::kRecoverable)) {
    return false;
  }
  return !CheckJvmtiError(jvmti, jvmti->AddCapabilities(&caps),
                          "AddCapabilities", JvmtiSeverity::kRecoverable);
}

// ------------------------------------------------ hidden-api enforcement
// Best-effort: disable ART's hidden-API enforcement so the payload (a clean
// child of the app class loader) can reflectively reach framework internals
// such as android.view.View#getUniqueDrawingId or the WindowInspector. This is
// the same JVMTI extension-function mechanism used by HiddenApiSilencer in
// tools-base/transport/native/jvmti/hidden_api_silencer.cc:30. It is purely
// additive: if the extension is unavailable we log and continue (the app is
// debuggable, so most APIs are reachable regardless).
enum class HiddenApiResult { kDisabled, kUnavailable, kFailed };

HiddenApiResult DisableHiddenApiEnforcement(jvmtiEnv* jvmti) {
  jint count = 0;
  jvmtiExtensionFunctionInfo* extensions = nullptr;
  if (CheckJvmtiError(jvmti, jvmti->GetExtensionFunctions(&count, &extensions),
                      "GetExtensionFunctions", JvmtiSeverity::kRecoverable) ||
      extensions == nullptr) {
    VS_LOGW("Hidden-API extension functions unavailable; continuing");
    return HiddenApiResult::kUnavailable;
  }

  jvmtiExtensionFunction disable_fn = nullptr;
  for (jint i = 0; i < count; ++i) {
    const jvmtiExtensionFunctionInfo& ext = extensions[i];
    if (ext.id != nullptr &&
        std::strcmp(
            "com.android.art.misc.disable_hidden_api_enforcement_policy",
            ext.id) == 0) {
      disable_fn = ext.func;
    }
  }

  HiddenApiResult result = HiddenApiResult::kUnavailable;
  if (disable_fn != nullptr) {
    const jvmtiError err = disable_fn(jvmti);
    if (!CheckJvmtiError(jvmti, err, "disable_hidden_api_enforcement_policy",
                         JvmtiSeverity::kRecoverable)) {
      VS_LOGI("Hidden-API enforcement disabled for this process");
      result = HiddenApiResult::kDisabled;
    } else {
      result = HiddenApiResult::kFailed;
    }
  } else {
    VS_LOGW(
        "disable_hidden_api_enforcement_policy extension not found; "
        "continuing without silencing");
  }

  // Free the extension descriptor table (matches HiddenApiSilencer::Setup).
  for (jint i = 0; i < count; ++i) {
    jvmtiExtensionFunctionInfo& ext = extensions[i];
    if (ext.params != nullptr) {
      for (jint j = 0; j < ext.param_count; ++j) {
        jvmti->Deallocate(reinterpret_cast<unsigned char*>(ext.params[j].name));
      }
      jvmti->Deallocate(reinterpret_cast<unsigned char*>(ext.params));
    }
    jvmti->Deallocate(reinterpret_cast<unsigned char*>(ext.short_description));
    jvmti->Deallocate(reinterpret_cast<unsigned char*>(ext.errors));
    jvmti->Deallocate(reinterpret_cast<unsigned char*>(ext.id));
  }
  jvmti->Deallocate(reinterpret_cast<unsigned char*>(extensions));
  return result;
}

// ----------------------------------------------------------------- jni env
// Returns a JNIEnv for the current thread, attaching it to the VM if needed.
// |attached_out| is set to true when this call performed the attach, so the
// caller can detach symmetrically. Mirrors profiler::GetThreadLocalJNI in
// jvmti_helper.cc:76.
JNIEnv* GetThreadJni(JavaVM* vm, bool* attached_out) {
  *attached_out = false;
  JNIEnv* jni = nullptr;
  const jint result =
      vm->GetEnv(reinterpret_cast<void**>(&jni), JNI_VERSION_1_6);
  if (result == JNI_OK && jni != nullptr) {
    return jni;
  }
  if (result == JNI_EDETACHED) {
    VS_LOGI("Current thread not attached to VM; attaching");
    if (vm->AttachCurrentThread(&jni, nullptr) != JNI_OK || jni == nullptr) {
      VS_LOGE("AttachCurrentThread failed");
      return nullptr;
    }
    *attached_out = true;
    return jni;
  }
  VS_LOGE("GetEnv(JNI_VERSION_1_6) failed: %d", result);
  return nullptr;
}

// Logs and clears any pending JNI exception. Returns true if one was pending.
bool ClearPendingException(JNIEnv* env, const char* what) {
  if (env->ExceptionCheck()) {
    VS_LOGE("Pending JNI exception during %s", what);
    env->ExceptionDescribe();
    env->ExceptionClear();
    return true;
  }
  return false;
}

// ----------------------------------------------------------------- core
// Detaches the current thread on scope exit when this agent attached it.
struct ThreadDetacher {
  JavaVM* vm;
  bool attached;
  ~ThreadDetacher() {
    // The agent thread that runs Payload is spawned by the Java side, so
    // nothing here needs to stay attached.
    if (attached) vm->DetachCurrentThread();
  }
};

// Disposes the JVMTI env (releasing any capabilities it holds) on scope exit,
// unless Dispose() already did.
struct JvmtiEnvDisposer {
  jvmtiEnv* jvmti;
  void Dispose() {
    if (jvmti == nullptr) return;
    CheckJvmtiError(jvmti, jvmti->DisposeEnvironment(), "DisposeEnvironment",
                    JvmtiSeverity::kRecoverable);
    jvmti = nullptr;
  }
  ~JvmtiEnvDisposer() { Dispose(); }
};

// Looks up Bootstrap through the system class loader (which delegates to the
// boot class path). Returns nullptr, with the pending exception cleared, when
// it is not loadable (yet). |quiet| skips the log for the expected first miss.
jclass FindBootstrapClass(JNIEnv* env, bool quiet) {
  jclass cls = env->FindClass(kBootstrapClassName);
  if (env->ExceptionCheck()) {
    if (quiet) {
      env->ExceptionClear();
    } else {
      ClearPendingException(env, "FindClass(Bootstrap)");
    }
    return nullptr;
  }
  return cls;
}

// The attach path (`cmd activity attach-agent <pkg> <so>=<options>`). Returns
// JNI_OK on success, JNI_ERR otherwise.
jint InstallAgent(JavaVM* vm, char* options) {
  VS_LOGI("ViewSpector native agent attaching (options='%s')",
          options == nullptr ? "<null>" : options);

  AgentOptions parsed;
  if (!ParseOptions(options, &parsed)) {
    return JNI_ERR;
  }
  VS_LOGI("Parsed options: bootstrapDex='%s' payload='%s' socket='%s'",
          parsed.bootstrap_dex_path.c_str(), parsed.payload_path.c_str(),
          parsed.socket_name.c_str());

  // Ensure this thread can talk to the VM before creating the JVMTI env;
  // otherwise GetEnv for JVMTI can fail with JNI_EDETACHED.
  bool we_attached = false;
  JNIEnv* env = GetThreadJni(vm, &we_attached);
  if (env == nullptr) {
    return JNI_ERR;
  }
  ThreadDetacher detacher{vm, we_attached};

  jvmtiEnv* jvmti = CreateJvmtiEnv(vm);
  if (jvmti == nullptr) {
    return JNI_ERR;
  }
  JvmtiEnvDisposer disposer{jvmti};

  // Step 2, with no capabilities. Should this ART refuse without them, retry
  // once with the potential set (relinquished when the env is disposed below).
  if (DisableHiddenApiEnforcement(jvmti) == HiddenApiResult::kFailed) {
    VS_LOGW("Retrying hidden-API silencing with the potential capabilities added");
    if (AddPotentialCapabilities(jvmti)) {
      DisableHiddenApiEnforcement(jvmti);
    }
  }

  // Step 3: make the bootstrap dex visible to the bootstrap class loader so the
  // upcoming FindClass can resolve com/oberkfell/viewspector/agent/Bootstrap.
  // A re-injection finds it already there and must not append it again.
  jclass bootstrap_class = FindBootstrapClass(env, /*quiet=*/true);
  if (bootstrap_class != nullptr) {
    VS_LOGI("Bootstrap already on the boot class path (earlier attach); not re-appending");
  } else {
    VS_LOGI("AddToBootstrapClassLoaderSearch('%s')",
            parsed.bootstrap_dex_path.c_str());
    if (CheckJvmtiError(
            jvmti,
            jvmti->AddToBootstrapClassLoaderSearch(
                parsed.bootstrap_dex_path.c_str()),
            "AddToBootstrapClassLoaderSearch", JvmtiSeverity::kFatal)) {
      return JNI_ERR;
    }
  }

  // Step 4: both JVMTI effects are process-wide; the env is no longer needed.
  disposer.Dispose();

  // Step 5: locate Bootstrap and its static initialize(String,String)V.
  if (bootstrap_class == nullptr) {
    bootstrap_class = FindBootstrapClass(env, /*quiet=*/false);
  }
  if (bootstrap_class == nullptr) {
    VS_LOGE("Could not find class %s", kBootstrapClassName);
    return JNI_ERR;
  }

  jmethodID initialize = env->GetStaticMethodID(
      bootstrap_class, kInitializeMethodName, kInitializeMethodSignature);
  if (ClearPendingException(env, "GetStaticMethodID(initialize)") ||
      initialize == nullptr) {
    VS_LOGE("Could not find %s.%s%s", kBootstrapClassName,
            kInitializeMethodName, kInitializeMethodSignature);
    env->DeleteLocalRef(bootstrap_class);
    return JNI_ERR;
  }

  jstring payload_arg = env->NewStringUTF(parsed.payload_path.c_str());
  jstring socket_arg = env->NewStringUTF(parsed.socket_name.c_str());
  if (ClearPendingException(env, "NewStringUTF(args)") ||
      payload_arg == nullptr || socket_arg == nullptr) {
    VS_LOGE("Failed to build Java string arguments for initialize");
    if (payload_arg != nullptr) env->DeleteLocalRef(payload_arg);
    if (socket_arg != nullptr) env->DeleteLocalRef(socket_arg);
    env->DeleteLocalRef(bootstrap_class);
    return JNI_ERR;
  }

  VS_LOGI("Calling %s.%s(payload='%s', socket='%s')", kBootstrapClassName,
          kInitializeMethodName, parsed.payload_path.c_str(),
          parsed.socket_name.c_str());
  env->CallStaticVoidMethod(bootstrap_class, initialize, payload_arg,
                            socket_arg);

  jint rc = JNI_OK;
  if (ClearPendingException(env, "Bootstrap.initialize")) {
    VS_LOGE("Bootstrap.initialize threw; agent install failed");
    rc = JNI_ERR;
  } else {
    VS_LOGI("ViewSpector native agent installed successfully");
  }

  env->DeleteLocalRef(payload_arg);
  env->DeleteLocalRef(socket_arg);
  env->DeleteLocalRef(bootstrap_class);
  return rc;
}

}  // namespace

// ----------------------------------------------------------------- entrypoints
// attach-agent path: `cmd activity attach-agent <pkg> <so>=<options>`.
extern "C" JNIEXPORT jint JNICALL Agent_OnAttach(JavaVM* vm, char* options,
                                                 void* /*reserved*/) {
  return InstallAgent(vm, options);
}

// -agentpath path (load at VM start): NOT supported. Agent_OnLoad runs in the
// OnLoad phase, before the VM has started and long before the app exists, so
// FindClass / Bootstrap.initialize (which needs the app's class loader) cannot
// work there. Refuse clearly instead of failing halfway through the install.
extern "C" JNIEXPORT jint JNICALL Agent_OnLoad(JavaVM* /*vm*/, char* options,
                                               void* /*reserved*/) {
  VS_LOGE(
      "ViewSpector cannot load at VM start (-agentpath / Agent_OnLoad, "
      "options='%s'); attach it to the running app instead: "
      "adb shell cmd activity attach-agent <package> "
      "<path>/libviewspector.so=<bootstrap.dex>:<payload.jar>:<socket>",
      options == nullptr ? "<null>" : options);
  return JNI_ERR;
}

// Symmetric unload hook. The payload owns its own LocalServerSocket/thread and
// is torn down via the ShutdownCommand, so there is nothing to release here.
extern "C" JNIEXPORT void JNICALL Agent_OnUnload(JavaVM* /*vm*/) {
  VS_LOGI("ViewSpector native agent unloaded");
}
