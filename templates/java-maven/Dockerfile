# syntax=docker/dockerfile:1.7
# Java Maven 多阶段构建模板；平台复制后允许管理员按可信项目修改。
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /workspace
COPY pom.xml ./
RUN --mount=type=cache,target=/root/.m2 mvn -B -DskipTests dependency:go-offline
COPY . .
RUN --mount=type=cache,target=/root/.m2 mvn -B -DskipTests package

FROM eclipse-temurin:21-jre
ARG JAR_PATH=target/*.jar
RUN useradd --system --uid 10001 --create-home app
WORKDIR /app
COPY --from=build /workspace/${JAR_PATH} /app/app.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
