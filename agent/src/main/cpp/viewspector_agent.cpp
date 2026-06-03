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
 *   1. GetEnv a stand-alone JVMTI 1.2 environment and add all potential caps.
 *   2. (Best effort) silence hidden-API enforcement so the payload may reach
 *      framework internals (WindowInspector, getUniqueDrawingId, Inspection
 *      companions, ...). Same mechanism as HiddenApiSilencer in tools-base.
 *   3. AddToBootstrapClassLoaderSearch(bootstrapDexPath).
 *   4. FindClass com/oberkfell/viewspector/agent/Bootstrap, get its static
 *      initialize(String payloadPath, String socketName)V and call it.
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

// Logs and returns true when err is not JVMTI_ERROR_NONE. Mirrors
// profiler::CheckJvmtiError in jvmti_helper.cc:47.
bool CheckJvmtiError(jvmtiEnv* jvmti, jvmtiError err, const char* what) {
  if (err == JVMTI_ERROR_NONE) {
    return false;
  }
  char* name = nullptr;
  jvmti->GetErrorName(err, &name);
  VS_LOGE("JVMTI error %d(%s) during %s", err,
          name == nullptr ? "Unknown" : name, what);
  if (name != nullptr) {
    jvmti->Deallocate(reinterpret_cast<unsigned char*>(name));
  }
  return true;
}

// Adds all potential capabilities. Mirrors profiler::SetAllCapabilities in
// jvmti_helper.cc:61. We don't strictly need the bytecode-rewriting caps, but
// requesting the full potential set is harmless on a debuggable app and matches
// the reference agents.
void AddAllCapabilities(jvmtiEnv* jvmti) {
  jvmtiCapabilities caps;
  std::memset(&caps, 0, sizeof(caps));
  if (CheckJvmtiError(jvmti, jvmti->GetPotentialCapabilities(&caps),
                      "GetPotentialCapabilities")) {
    return;
  }
  CheckJvmtiError(jvmti, jvmti->AddCapabilities(&caps), "AddCapabilities");
}

// ------------------------------------------------ hidden-api enforcement
// Best-effort: disable ART's hidden-API enforcement so the payload (a clean
// child of the app class loader) can reflectively reach framework internals
// such as android.view.View#getUniqueDrawingId or the WindowInspector. This is
// the same JVMTI extension-function mechanism used by HiddenApiSilencer in
// tools-base/transport/native/jvmti/hidden_api_silencer.cc:30. It is purely
// additive: if the extension is unavailable we log and continue (the app is
// debuggable, so most APIs are reachable regardless).
void DisableHiddenApiEnforcement(jvmtiEnv* jvmti) {
  jint count = 0;
  jvmtiExtensionFunctionInfo* extensions = nullptr;
  if (CheckJvmtiError(jvmti, jvmti->GetExtensionFunctions(&count, &extensions),
                      "GetExtensionFunctions") ||
      extensions == nullptr) {
    VS_LOGW("Hidden-API extension functions unavailable; continuing");
    return;
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

  if (disable_fn != nullptr) {
    const jvmtiError err = disable_fn(jvmti);
    if (!CheckJvmtiError(jvmti, err, "disable_hidden_api_enforcement_policy")) {
      VS_LOGI("Hidden-API enforcement disabled for this process");
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
// Shared body for both OnAttach (cmd activity attach-agent) and OnLoad
// (-agentpath). Returns JNI_OK on success, JNI_ERR otherwise.
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

  jvmtiEnv* jvmti = CreateJvmtiEnv(vm);
  if (jvmti == nullptr) {
    if (we_attached) vm->DetachCurrentThread();
    return JNI_ERR;
  }

  AddAllCapabilities(jvmti);
  DisableHiddenApiEnforcement(jvmti);

  // Step 3: make the bootstrap dex visible to the bootstrap class loader so the
  // upcoming FindClass can resolve com/oberkfell/viewspector/agent/Bootstrap.
  VS_LOGI("AddToBootstrapClassLoaderSearch('%s')",
          parsed.bootstrap_dex_path.c_str());
  if (CheckJvmtiError(
          jvmti,
          jvmti->AddToBootstrapClassLoaderSearch(
              parsed.bootstrap_dex_path.c_str()),
          "AddToBootstrapClassLoaderSearch")) {
    if (we_attached) vm->DetachCurrentThread();
    return JNI_ERR;
  }

  // Step 4: locate Bootstrap and its static initialize(String,String)V.
  jclass bootstrap_class = env->FindClass(kBootstrapClassName);
  if (ClearPendingException(env, "FindClass(Bootstrap)") ||
      bootstrap_class == nullptr) {
    VS_LOGE("Could not find class %s", kBootstrapClassName);
    if (we_attached) vm->DetachCurrentThread();
    return JNI_ERR;
  }

  jmethodID initialize = env->GetStaticMethodID(
      bootstrap_class, kInitializeMethodName, kInitializeMethodSignature);
  if (ClearPendingException(env, "GetStaticMethodID(initialize)") ||
      initialize == nullptr) {
    VS_LOGE("Could not find %s.%s%s", kBootstrapClassName,
            kInitializeMethodName, kInitializeMethodSignature);
    env->DeleteLocalRef(bootstrap_class);
    if (we_attached) vm->DetachCurrentThread();
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
    if (we_attached) vm->DetachCurrentThread();
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

  // Detach only if we performed the attach; the agent thread that ran Payload
  // is spawned by the Java side, so nothing here needs to stay attached.
  if (we_attached) {
    vm->DetachCurrentThread();
  }
  return rc;
}

}  // namespace

// ----------------------------------------------------------------- entrypoints
// attach-agent path: `cmd activity attach-agent <pkg> <so>=<options>`.
extern "C" JNIEXPORT jint JNICALL Agent_OnAttach(JavaVM* vm, char* options,
                                                 void* /*reserved*/) {
  return InstallAgent(vm, options);
}

// -agentpath path (load at VM start): delegates to the same install body.
extern "C" JNIEXPORT jint JNICALL Agent_OnLoad(JavaVM* vm, char* options,
                                               void* /*reserved*/) {
  return InstallAgent(vm, options);
}

// Symmetric unload hook. The payload owns its own LocalServerSocket/thread and
// is torn down via the ShutdownCommand, so there is nothing to release here.
extern "C" JNIEXPORT void JNICALL Agent_OnUnload(JavaVM* /*vm*/) {
  VS_LOGI("ViewSpector native agent unloaded");
}
