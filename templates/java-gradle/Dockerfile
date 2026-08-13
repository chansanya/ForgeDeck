# syntax=docker/dockerfile:1.7
# Java Gradle 多阶段构建模板；平台复制后允许管理员按可信项目修改。
FROM gradle:8-jdk21 AS build
WORKDIR /workspace
COPY --chown=gradle:gradle . .
RUN --mount=type=cache,target=/home/gradle/.gradle gradle clean build -x test --no-daemon

FROM eclipse-temurin:21-jre
ARG JAR_PATH=build/libs/*.jar
RUN useradd --system --uid 10001 --create-home app
WORKDIR /app
COPY --from=build /workspace/${JAR_PATH} /app/app.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
