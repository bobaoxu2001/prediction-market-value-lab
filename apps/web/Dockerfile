# The Next.js site, as the docker-compose "web" service.
#
# NEXT_PUBLIC_API_BASE is inlined at BUILD time (Next inlines NEXT_PUBLIC_*
# variables into the client bundle), so it travels through a build arg; the
# compose file passes the host-published API address, which is what a browser
# on the host machine can actually reach.
FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

RUN npm run build

EXPOSE 3000
CMD ["npm", "start"]
