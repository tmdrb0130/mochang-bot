from flask import Flask, request, jsonify, render_template_string
import requests
import os
import base64
import time
from dotenv import load_dotenv

# -----------------------------------------
# 환경설정
# -----------------------------------------

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)

# 업로드 최대 크기: 10MB
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


# -----------------------------------------
# 모델 목록
# -----------------------------------------

MODELS = {

    "glm": {
        "name": "GLM 5.2",
        "id": "z-ai/glm-5.2:free"
    },

    "deepseek": {
        "name": "DeepSeek V3",
        "id": "deepseek/deepseek-chat:free"
    },

    "nemotron": {
        "name": "Nemotron 3 Super",
        "id": (
            "nvidia/"
            "nemotron-3-super-120b-a12b:free"
        )
    },

    "minimax": {
        "name": "MiniMax M3",
        "id": "minimax/minimax-m3:free"
    },

    "free": {
        "name": "OpenRouter Free",
        "id": "openrouter/free"
    }
}


# -----------------------------------------
# 웹페이지
# -----------------------------------------

HTML = r"""
<!DOCTYPE html>

<html lang="ko">

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>AI Model Test</title>


<style>

* {
    box-sizing: border-box;
}


body {

    margin: 0;

    font-family:
        Arial,
        "Malgun Gothic",
        sans-serif;

    background: #f2f3f5;

    height: 100vh;

    display: flex;

    justify-content: center;

    align-items: center;
}


.container {

    width: 1100px;

    max-width: 95%;

    height: 92vh;

    background: white;

    border-radius: 18px;

    box-shadow:
        0 10px 35px rgba(0,0,0,0.10);

    overflow: hidden;

    display: flex;

    flex-direction: column;
}


/* =========================================
   HEADER
========================================= */

.header {

    background: #222;

    color: white;

    padding: 16px 22px;

    display: flex;

    justify-content: space-between;

    align-items: center;

}


.title {

    font-size: 20px;

    font-weight: bold;
}


.model-select {

    display: flex;

    align-items: center;

    gap: 8px;

    font-size: 14px;
}


.model-select select {

    border: none;

    border-radius: 8px;

    padding: 8px 12px;

    font-size: 14px;

    cursor: pointer;
}


/* =========================================
   CHAT
========================================= */

.messages {

    flex: 1;

    overflow-y: auto;

    padding: 24px;

    background: #fafafa;
}


.message {

    display: flex;

    margin-bottom: 20px;
}


.message.user {

    justify-content: flex-end;
}


.bubble {

    max-width: 78%;

    border-radius: 15px;

    padding: 13px 16px;

    line-height: 1.6;

    white-space: pre-wrap;

    word-break: break-word;
}


.user .bubble {

    background: #222;

    color: white;

    border-bottom-right-radius: 4px;
}


.assistant .bubble {

    background: #e9eaec;

    color: #222;

    border-bottom-left-radius: 4px;
}


.file-info {

    margin-top: 8px;

    font-size: 12px;

    opacity: 0.75;
}


/* =========================================
   DROP ZONE
========================================= */

.drop-zone {

    margin: 0 18px 12px 18px;

    border: 2px dashed #c7c7c7;

    border-radius: 12px;

    min-height: 100px;

    display: flex;

    justify-content: center;

    align-items: center;

    text-align: center;

    color: #777;

    cursor: pointer;

    transition: 0.2s;

    background: #fcfcfc;
}


.drop-zone.dragover {

    border-color: #222;

    background: #f0f0f0;

    color: #222;
}


.drop-content {

    padding: 18px;
}


.drop-title {

    font-size: 15px;

    font-weight: bold;

    margin-bottom: 6px;
}


.drop-sub {

    font-size: 12px;

    color: #888;
}


#fileInput {

    display: none;
}


/* =========================================
   PREVIEW
========================================= */

.preview-container {

    display: none;

    margin: 0 18px 12px 18px;

    padding: 12px;

    border-radius: 10px;

    background: #f0f0f0;

    position: relative;
}


.preview-container img {

    max-width: 180px;

    max-height: 140px;

    border-radius: 8px;

    display: block;
}


.file-name {

    margin-top: 7px;

    font-size: 12px;

    color: #555;
}


.remove-file {

    position: absolute;

    top: 6px;

    right: 8px;

    border: none;

    background: #333;

    color: white;

    width: 24px;

    height: 24px;

    border-radius: 50%;

    cursor: pointer;
}


/* =========================================
   STATUS
========================================= */

.status {

    min-height: 28px;

    padding: 6px 20px;

    border-top: 1px solid #eee;

    background: #f7f7f7;

    font-size: 12px;

    color: #777;
}


/* =========================================
   INPUT
========================================= */

.input-area {

    padding: 14px;

    display: flex;

    gap: 10px;

    border-top: 1px solid #ddd;

    background: white;
}


textarea {

    flex: 1;

    height: 65px;

    resize: none;

    border: 1px solid #ccc;

    border-radius: 10px;

    padding: 12px;

    font-size: 15px;

    outline: none;
}


.send-button {

    width: 90px;

    border: none;

    border-radius: 10px;

    background: #222;

    color: white;

    font-size: 15px;

    cursor: pointer;
}


.send-button:disabled {

    background: #999;

    cursor: not-allowed;
}


/* =========================================
   MOBILE
========================================= */

@media (max-width: 700px) {

    .container {

        height: 100vh;

        max-width: 100%;

        border-radius: 0;
    }

    .header {

        flex-direction: column;

        align-items: flex-start;

        gap: 10px;
    }

    .bubble {

        max-width: 90%;
    }

}

</style>

</head>


<body>


<div class="container">


    <!-- =====================================
         HEADER
    ====================================== -->

    <div class="header">

        <div class="title">
            AI Model Test
        </div>


        <div class="model-select">

            <span>모델</span>

            <select id="modelSelect">

                <option value="minimax">
                    MiniMax M3
                </option>

                <option value="glm">
                    GLM 5.2
                </option>

                <option value="deepseek">
                    DeepSeek V3
                </option>

                <option value="nemotron">
                    Nemotron 3 Super
                </option>

                <option value="free">
                    OpenRouter Free
                </option>

            </select>

        </div>

    </div>


    <!-- =====================================
         MESSAGES
    ====================================== -->

    <div id="messages"
         class="messages">

        <div class="message assistant">

            <div class="bubble">

안녕하세요.

위에서 AI 모델을 선택할 수 있습니다.

이미지는 아래 영역으로 끌어다 놓고
질문과 함께 전송할 수 있습니다.

            </div>

        </div>

    </div>


    <!-- =====================================
         DROP ZONE
    ====================================== -->

    <div id="dropZone"
         class="drop-zone">

        <div class="drop-content">

            <div class="drop-title">

                📎 이미지를 여기에 끌어다 놓으세요

            </div>

            <div class="drop-sub">

                또는 클릭해서 파일을 선택하세요
                · JPG / PNG / WEBP
                · 최대 10MB

            </div>

        </div>

    </div>


    <input
        id="fileInput"
        type="file"
        accept="image/*"
    >


    <!-- =====================================
         PREVIEW
    ====================================== -->

    <div id="previewContainer"
         class="preview-container">

        <button
            class="remove-file"
            onclick="removeFile()">

            ×

        </button>

        <img
            id="previewImage"
            src=""
            alt="preview">

        <div id="fileName"
             class="file-name">
        </div>

    </div>


    <!-- =====================================
         STATUS
    ====================================== -->

    <div id="status"
         class="status">

        준비됨

    </div>


    <!-- =====================================
         INPUT
    ====================================== -->

    <div class="input-area">

        <textarea
            id="messageInput"
            placeholder="질문을 입력하세요..."
            onkeydown="handleKey(event)">
        </textarea>


        <button
            id="sendButton"
            class="send-button"
            onclick="sendMessage()">

            전송

        </button>

    </div>


</div>


<script>


// ========================================
// 전역 변수
// ========================================

let selectedFile = null;


// ========================================
// DOM
// ========================================

const dropZone =
    document.getElementById("dropZone");

const fileInput =
    document.getElementById("fileInput");

const previewContainer =
    document.getElementById("previewContainer");

const previewImage =
    document.getElementById("previewImage");

const fileName =
    document.getElementById("fileName");


// ========================================
// 클릭해서 파일 선택
// ========================================

dropZone.addEventListener(
    "click",
    function() {

        fileInput.click();

    }
);


fileInput.addEventListener(
    "change",
    function(event) {

        if (event.target.files.length > 0) {

            handleFile(
                event.target.files[0]
            );

        }

    }
);


// ========================================
// 드래그 시작
// ========================================

dropZone.addEventListener(
    "dragover",
    function(event) {

        event.preventDefault();

        dropZone.classList.add(
            "dragover"
        );

    }
);


// ========================================
// 드래그 종료
// ========================================

dropZone.addEventListener(
    "dragleave",
    function(event) {

        event.preventDefault();

        dropZone.classList.remove(
            "dragover"
        );

    }
);


// ========================================
// 파일 드롭
// ========================================

dropZone.addEventListener(
    "drop",
    function(event) {

        event.preventDefault();

        dropZone.classList.remove(
            "dragover"
        );


        if (
            event.dataTransfer.files.length
            > 0
        ) {

            handleFile(
                event.dataTransfer.files[0]
            );

        }

    }
);


// ========================================
// 파일 처리
// ========================================

function handleFile(file) {


    // 이미지인지 확인

    if (!file.type.startsWith("image/")) {

        alert(
            "현재는 이미지 파일만 지원합니다."
        );

        return;

    }


    // 10MB 제한

    if (file.size > 10 * 1024 * 1024) {

        alert(
            "파일 크기는 10MB 이하만 가능합니다."
        );

        return;

    }


    selectedFile = file;


    // 파일 미리보기

    const reader =
        new FileReader();


    reader.onload =
        function(event) {

            previewImage.src =
                event.target.result;

            fileName.textContent =
                file.name;

            previewContainer.style.display =
                "block";

            dropZone.style.display =
                "none";

        };


    reader.readAsDataURL(file);

}


// ========================================
// 파일 제거
// ========================================

function removeFile() {

    selectedFile = null;

    previewImage.src = "";

    fileName.textContent = "";

    previewContainer.style.display =
        "none";

    dropZone.style.display =
        "flex";

    fileInput.value = "";

}


// ========================================
// 메시지 추가
// ========================================

function addMessage(
    text,
    type,
    fileNameText = null
) {

    const messages =
        document.getElementById(
            "messages"
        );


    const message =
        document.createElement(
            "div"
        );

    message.className =
        "message " + type;


    const bubble =
        document.createElement(
            "div"
        );

    bubble.className =
        "bubble";


    bubble.textContent =
        text;


    if (fileNameText) {

        const info =
            document.createElement(
                "div"
            );

        info.className =
            "file-info";

        info.textContent =
            "📎 " + fileNameText;

        bubble.appendChild(info);

    }


    message.appendChild(
        bubble
    );


    messages.appendChild(
        message
    );


    messages.scrollTop =
        messages.scrollHeight;
}


// ========================================
// Enter 처리
// ========================================

function handleKey(event) {

    if (
        event.key === "Enter"
        &&
        !event.shiftKey
    ) {

        event.preventDefault();

        sendMessage();

    }

}


// ========================================
// 메시지 전송
// ========================================

async function sendMessage() {


    const input =
        document.getElementById(
            "messageInput"
        );


    const button =
        document.getElementById(
            "sendButton"
        );


    const status =
        document.getElementById(
            "status"
        );


    const modelSelect =
        document.getElementById(
            "modelSelect"
        );


    const message =
        input.value.trim();


    // 질문도 없고 파일도 없으면 종료

    if (
        !message
        &&
        !selectedFile
    ) {

        return;

    }


    const model =
        modelSelect.value;


    const modelName =
        modelSelect
            .options[
                modelSelect.selectedIndex
            ]
            .text;


    // 사용자 메시지 표시

    addMessage(
        message || "이미지를 분석해주세요.",
        "user",
        selectedFile
            ? selectedFile.name
            : null
    );


    input.value = "";


    button.disabled =
        true;


    button.textContent =
        "생성중...";


    status.textContent =
        modelName +
        " 응답 생성 중...";


    // FormData 생성

    const formData =
        new FormData();


    formData.append(
        "message",
        message
    );


    formData.append(
        "model",
        model
    );


    if (selectedFile) {

        formData.append(
            "file",
            selectedFile
        );

    }


    // 전송 시작

    const startTime =
        performance.now();


    try {


        const response =
            await fetch(
                "/chat",
                {

                    method: "POST",

                    body:
                        formData

                }
            );


        const data =
            await response.json();


        const elapsed =
            (
                performance.now()
                - startTime
            ) / 1000;


        // 정상 응답

        if (
            response.ok
            &&
            data.answer
        ) {


            addMessage(
                data.answer,
                "assistant"
            );


            status.textContent =
                "모델: " +
                (
                    data.model ||
                    modelName
                ) +
                " · 응답시간: " +
                elapsed.toFixed(2) +
                "초";


        }

        else {


            let errorText =
                "오류가 발생했습니다.";


            if (data.error) {

                if (
                    typeof data.error
                    === "string"
                ) {

                    errorText +=
                        "\n" +
                        data.error;

                }

                else {

                    errorText +=
                        "\n" +
                        JSON.stringify(
                            data.error,
                            null,
                            2
                        );

                }

            }


            addMessage(
                errorText,
                "assistant"
            );


            status.textContent =
                "오류 발생";


        }


    }

    catch (error) {


        addMessage(
            "서버 연결 오류:\n"
            + error,
            "assistant"
        );


        status.textContent =
            "서버 연결 오류";


    }


    finally {


        button.disabled =
            false;


        button.textContent =
            "전송";


        // 전송 후 파일 제거

        removeFile();

    }

}

document.addEventListener("paste", function(event) {

    const items = event.clipboardData.items;

    for (let i = 0; i < items.length; i++) {

        const item = items[i];

        // 클립보드에 이미지가 있는 경우
        if (item.type.startsWith("image/")) {

            const file = item.getAsFile();

            if (file) {

                handleFile(file);

                event.preventDefault();

                return;
            }
        }
    }
});

</script>


</body>

</html>
"""


# =========================================
# 메인 페이지
# =========================================

@app.route("/")
def home():

    return render_template_string(
        HTML
    )


# =========================================
# Chat API
# =========================================

@app.route(
    "/chat",
    methods=["POST"]
)
def chat():


    try:


        # -------------------------------
        # 입력
        # -------------------------------

        user_message = request.form.get(
                "message",
                ""
            )


        model_key = request.form.get(
                "model",
                "free"
            )


        uploaded_file = request.files.get(
                "file"
            )


        # -------------------------------
        # 모델 확인
        # -------------------------------

        if model_key not in MODELS:

            return jsonify({

                "error":
                    "잘못된 모델입니다."

            }), 400


        model_info = MODELS[
                model_key
            ]


        model_id = model_info["id"]


        model_name = model_info["name"]


        # -------------------------------
        # 메시지 구성
        # -------------------------------

        if uploaded_file:


            # MIME 타입 확인

            mime_type = uploaded_file.mimetype


            if not mime_type.startswith(
                "image/"
            ):

                return jsonify({

                    "error":
                        "이미지 파일만 지원합니다."

                }), 400


            # 파일 읽기

            file_bytes = uploaded_file.read()


            # Base64 변환

            encoded = base64.b64encode(
                    file_bytes
                ).decode(
                    "utf-8"
                )


            data_url = (
                    "data:"
                    + mime_type
                    + ";base64,"
                    + encoded
                )


            # 질문이 없으면 기본 질문

            if not user_message:

                user_message = "이 이미지를 자세히 분석해주세요."


            # OpenRouter multimodal format

            content = [

                {

                    "type":
                        "text",

                    "text":
                        user_message

                },

                {

                    "type":
                        "image_url",

                    "image_url": {

                        "url":
                            data_url

                    }

                }

            ]


        else:


            # 일반 텍스트

            content = user_message


        # -------------------------------
        # Payload
        # -------------------------------

        payload = {

            "model":
                model_id,

            "messages": [

                {

                    "role":
                        "user",

                    "content":
                        content

                }

            ]

        }


        # -------------------------------
        # Provider fallback
        # -------------------------------

        payload["provider"] = {

            "allow_fallbacks":
                True

        }


        # -------------------------------
        # OpenRouter 요청
        # -------------------------------

        headers = {

            "Authorization":
                f"Bearer {API_KEY}",

            "Content-Type":
                "application/json"

        }


        start_time = time.time()


        response = requests.post(

                OPENROUTER_URL,

                headers=headers,

                json=payload,

                timeout=180

            )


        elapsed = time.time() - start_time


        result = response.json()


        # -------------------------------
        # 서버 콘솔 출력
        # -------------------------------

        print("")
        print(
            "================================"
        )

        print(
            "모델:",
            model_name
        )

        print(
            "Model ID:",
            model_id
        )

        print(
            "파일:",
            uploaded_file.filename
            if uploaded_file
            else "없음"
        )

        print(
            "HTTP:",
            response.status_code
        )

        print(
            "응답시간:",
            round(
                elapsed,
                2
            ),
            "초"
        )

        print(
            result
        )

        print(
            "================================"
        )


        # -------------------------------
        # 정상 응답
        # -------------------------------

        if (
            response.status_code == 200
            and
            "choices" in result
        ):


            message_data = result[
                    "choices"
                ][0][
                    "message"
                ]


            answer = message_data.get(
                    "content",
                    ""
                )


            return jsonify({

                "answer":
                    answer,

                "model":
                    result.get(
                        "model",
                        model_name
                    ),

                "elapsed":
                    round(
                        elapsed,
                        2
                    )

            })


        # -------------------------------
        # OpenRouter 오류
        # -------------------------------

        return jsonify({

            "error":
                result

        }), response.status_code


    except Exception as e:


        print(
            "SERVER ERROR:",
            e
        )


        return jsonify({

            "error":
                str(e)

        }), 500


# =========================================
# 파일 크기 초과
# =========================================

@app.errorhandler(413)
def too_large(e):

    return jsonify({

        "error":
            "파일 크기가 너무 큽니다. 최대 10MB입니다."

    }), 413


# =========================================
# 서버 실행
# =========================================

if __name__ == "__main__":


    print("")
    print(
        "================================"
    )
    print(
        " AI Model Test Server"
    )
    print(
        "================================"
    )
    print("")


    for key, value in MODELS.items():

        print(
            f"{key:10} : "
            f"{value['id']}"
        )


    print("")

    print(
        "http://localhost:5000"
    )

    print("")


    app.run(

        host="0.0.0.0",

        port=5000,

        debug=False

    )

